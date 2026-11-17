import { RegimexRequestError, saveDownload } from "./regimexApi";
import type { Analysis, DownloadKind, RegimexApi, RegimexStatus } from "./regimexApi";
import type { AnalysisPreview } from "./regimexResult";

type Client = Pick<RegimexApi, "status" | "poll" | "submit" | "download" | "preview">;
type Schedule = (callback: () => void, milliseconds: number) => () => void;

export type AnalysisPanelState = {
  status: RegimexStatus | null;
  loading: boolean;
  submitting: boolean;
  downloading: DownloadKind | null;
  error: string;
  confirmationRequired: boolean;
  pollingPaused: boolean;
  preview: AnalysisPreview | null;
  previewLoading: boolean;
  previewError: string | null;
};

export function pending(analysis: Analysis | null): boolean {
  return analysis !== null && ["queued", "running", "cancel_requested"].includes(analysis.state);
}

function message(cause: unknown): string {
  return cause instanceof RegimexRequestError ? cause.message : "Не удалось связаться с сервисом анализа.";
}

function resourceUnavailable(cause: unknown): boolean {
  return cause instanceof RegimexRequestError && [401, 403, 404].includes(cause.status);
}

function previewTarget(status: RegimexStatus | null): string | null {
  return status?.enabled && status.eligible && status.analysis?.state === "succeeded"
    && status.analysis.result_available ? status.analysis.analysis_id : null;
}

const schedule: Schedule = (callback, milliseconds) => {
  const timer = setTimeout(callback, milliseconds);
  return () => clearTimeout(timer);
};

/** One run and authenticated session; owns requests, polling and late-response fencing. */
export class RegimexController {
  private client: Client;
  private saveFile: (blob: Blob, filename: string) => void;
  private schedule: Schedule;
  private listeners = new Set<() => void>();
  private state: AnalysisPanelState = {
    status: null, loading: true, submitting: false, downloading: null,
    error: "", confirmationRequired: false, pollingPaused: false,
    preview: null, previewLoading: false, previewError: null,
  };
  private lifetime = new AbortController();
  private request: AbortController | null = null;
  private previewRequest: AbortController | null = null;
  private previewGeneration = 0;
  private cancelPoll: (() => void) | null = null;
  private active = false;
  private generation = 0;
  private pollCount = 0;
  private selectedAnalysisId: string | null;

  constructor(client: Client, saveFile = saveDownload, schedulePoll = schedule, analysisId: string | null = null) {
    this.client = client;
    this.saveFile = saveFile;
    this.schedule = schedulePoll;
    this.selectedAnalysisId = analysisId;
  }

  getSnapshot = (): AnalysisPanelState => this.state;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  private update(patch: Partial<AnalysisPanelState>) {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((listener) => listener());
  }

  private setStatus(status: RegimexStatus | null, patch: Partial<AnalysisPanelState> = {}) {
    const identityChanged = previewTarget(status) !== previewTarget(this.state.status);
    if (identityChanged) this.cancelPreview();
    this.update({ ...patch, status,
      ...(identityChanged ? { preview: null, previewLoading: false, previewError: null } : {}),
    });
    if (previewTarget(status) && !this.state.preview && !this.state.previewLoading && !this.state.previewError) {
      void this.refreshPreview();
    }
  }

  private cancelPreview() {
    this.previewGeneration += 1;
    this.previewRequest?.abort();
    this.previewRequest = null;
  }

  async refreshPreview() {
    const analysisId = previewTarget(this.state.status);
    if (!this.active || !analysisId || this.state.previewLoading || this.state.preview) return;
    this.cancelPreview();
    const request = new AbortController();
    this.previewRequest = request;
    const generation = this.previewGeneration;
    const signal = AbortSignal.any([this.lifetime.signal, request.signal]);
    const current = () => this.active && !signal.aborted && generation === this.previewGeneration
      && previewTarget(this.state.status) === analysisId;
    this.update({ previewLoading: true, previewError: null });
    try {
      const preview = await this.client.preview(analysisId, signal);
      if (!current()) return;
      this.update({ preview, previewLoading: false });
    } catch (cause) {
      if (!current()) return;
      this.update({ previewError: message(cause), previewLoading: false });
    } finally {
      if (generation === this.previewGeneration) this.previewRequest = null;
    }
  }

  start() {
    if (this.active) return;
    this.active = true;
    this.lifetime = new AbortController();
    this.update({ submitting: false, downloading: null, previewLoading: false });
    void this.refresh();
  }

  stop() {
    this.active = false;
    this.generation += 1;
    this.request?.abort();
    this.cancelPreview();
    this.lifetime.abort();
    this.cancelPoll?.();
    this.cancelPoll = null;
  }

  private beginRequest(): { signal: AbortSignal; current: () => boolean } {
    this.cancelPoll?.();
    this.cancelPoll = null;
    this.request?.abort();
    this.request = new AbortController();
    const generation = ++this.generation;
    return {
      signal: AbortSignal.any([this.lifetime.signal, this.request.signal]),
      current: () => this.active && generation === this.generation,
    };
  }

  async refresh() {
    // While a POST is unconfirmed only replaying its key can identify the accepted job.
    if (!this.active || this.state.submitting || this.state.confirmationRequired) return;
    this.pollCount = 0;
    const request = this.beginRequest();
    this.update({ loading: true, error: "", pollingPaused: false });
    try {
      const status = await this.client.status(request.signal);
      if (!request.current()) return;
      if (status.enabled && status.eligible && this.selectedAnalysisId
        && status.analysis?.analysis_id !== this.selectedAnalysisId) {
        status.analysis = await this.client.poll(this.selectedAnalysisId, request.signal);
        if (!request.current()) return;
      }
      this.setStatus(status, { loading: false });
      this.pollLater();
    } catch (cause) {
      if (!request.current()) return;
      this.statusFailed(cause);
    }
  }

  private statusFailed(cause: unknown) {
    const patch = { loading: false, error: message(cause), pollingPaused: true };
    if (resourceUnavailable(cause)) {
      this.lifetime.abort();
      this.lifetime = new AbortController();
      this.setStatus(null, { ...patch, submitting: false, downloading: null, confirmationRequired: false });
    } else {
      this.update(patch);
    }
  }

  private pollLater() {
    if (!this.active || !this.state.status?.enabled || !this.state.status.eligible
      || !pending(this.state.status.analysis)) return;
    if (this.pollCount >= 40) {
      this.update({ pollingPaused: true });
      return;
    }
    const delay = Math.min(15_000, 2000 * 1.5 ** this.pollCount);
    this.cancelPoll = this.schedule(() => { void this.poll(); }, delay);
  }

  private async poll() {
    const analysis = this.state.status?.analysis;
    if (!this.active || !this.state.status?.enabled || !this.state.status.eligible
      || !analysis || !pending(analysis)) return;
    const request = this.beginRequest();
    this.pollCount += 1;
    try {
      const result = await this.client.poll(analysis.analysis_id, request.signal);
      if (!request.current() || !this.state.status) return;
      this.setStatus({ ...this.state.status, analysis: result }, { error: "", pollingPaused: false });
      this.pollLater();
    } catch (cause) {
      if (!request.current()) return;
      // Any failed poll needs a deliberate refresh; rate limits cannot form a retry loop.
      this.statusFailed(cause);
    }
  }

  async submit() {
    const status = this.state.status;
    if (!this.active || !status?.enabled || !status.eligible || this.state.loading
      || this.state.submitting || pending(status.analysis)
      || status.analysis?.state === "succeeded") return;
    const request = this.beginRequest();
    const retryOf = status.analysis?.analysis_id ?? null;
    this.update({ submitting: true, error: "", pollingPaused: false });
    try {
      const analysis = await this.client.submit(retryOf, request.signal);
      if (!request.current()) return;
      this.pollCount = 0;
      this.selectedAnalysisId = analysis.analysis_id;
      this.setStatus({ ...status, analysis }, { submitting: false, confirmationRequired: false });
      this.pollLater();
    } catch (cause) {
      if (!request.current()) return;
      if (resourceUnavailable(cause)) {
        this.statusFailed(cause);
        return;
      }
      // Trade rejects analysis_busy before dispatch. A later refused replay cannot
      // resolve an earlier lost acknowledgement, so keep its original retry_of.
      const uncertain = this.state.confirmationRequired || !(cause instanceof RegimexRequestError)
        || (cause.status >= 500 && !(cause.status === 503 && cause.code === "analysis_busy"));
      this.update({ submitting: false, confirmationRequired: uncertain, error: uncertain
        ? `${message(cause)} Подтверждение запуска не получено.` : message(cause) });
    }
  }

  async download(kind: DownloadKind) {
    const status = this.state.status;
    if (!this.active || this.state.downloading || !status?.enabled || !status.eligible) return;
    const analysis = status.analysis;
    if (kind === "report" && (!analysis?.report_available || analysis.state !== "succeeded")) return;
    const lifetime = this.lifetime;
    this.update({ downloading: kind, error: "" });
    try {
      const blob = await this.client.download(kind, analysis?.analysis_id ?? null, lifetime.signal);
      if (!this.active || lifetime.signal.aborted) return;
      const filename = kind === "report" ? `regimex-${analysis!.analysis_id}.html` : "regimex-export.json";
      this.saveFile(blob, filename);
    } catch (cause) {
      if (this.active && !lifetime.signal.aborted) this.update({ error: message(cause) });
    } finally {
      if (this.active && !lifetime.signal.aborted) this.update({ downloading: null });
    }
  }
}
