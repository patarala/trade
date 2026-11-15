import { RegimexRequestError, saveDownload } from "./regimexApi";
import type { Analysis, DownloadKind, RegimexApi, RegimexStatus } from "./regimexApi";

type Client = Pick<RegimexApi, "status" | "poll" | "submit" | "download">;
type Schedule = (callback: () => void, milliseconds: number) => () => void;

export type AnalysisPanelState = {
  status: RegimexStatus | null;
  loading: boolean;
  submitting: boolean;
  downloading: DownloadKind | null;
  error: string;
  confirmationRequired: boolean;
  pollingPaused: boolean;
};

export function pending(analysis: Analysis | null): boolean {
  return analysis !== null && ["queued", "running", "cancel_requested"].includes(analysis.state);
}

function message(cause: unknown): string {
  return cause instanceof RegimexRequestError ? cause.message : "Не удалось связаться с сервисом анализа.";
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
  };
  private lifetime = new AbortController();
  private request: AbortController | null = null;
  private cancelPoll: (() => void) | null = null;
  private active = false;
  private generation = 0;
  private pollCount = 0;

  constructor(client: Client, saveFile = saveDownload, schedulePoll = schedule) {
    this.client = client;
    this.saveFile = saveFile;
    this.schedule = schedulePoll;
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

  start() {
    if (this.active) return;
    this.active = true;
    this.lifetime = new AbortController();
    this.update({ submitting: false, downloading: null });
    void this.refresh();
  }

  stop() {
    this.active = false;
    this.generation += 1;
    this.request?.abort();
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
    if (!this.active || this.state.submitting) return;
    this.pollCount = 0;
    this.update({ pollingPaused: false });
    await this.loadStatus();
  }

  private async loadStatus(uncertainMessage = "") {
    const request = this.beginRequest();
    this.update({ loading: true, error: uncertainMessage });
    try {
      const status = await this.client.status(request.signal);
      if (!request.current()) return;
      const unresolved = uncertainMessage && (!status.analysis || ["failed", "cancelled"].includes(status.analysis.state));
      this.update({ status, loading: false, confirmationRequired: false, error: unresolved ? uncertainMessage : "" });
      this.pollLater();
    } catch (cause) {
      if (!request.current()) return;
      this.update({ loading: false, error: message(cause), pollingPaused: true });
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
      this.update({ status: { ...this.state.status, analysis: result }, error: "", pollingPaused: false });
      this.pollLater();
    } catch (cause) {
      if (!request.current()) return;
      // Any failed poll needs a deliberate refresh; rate limits cannot form a retry loop.
      this.update({ error: message(cause), pollingPaused: true });
    }
  }

  async submit() {
    const status = this.state.status;
    if (!this.active || !status?.enabled || !status.eligible || this.state.loading
      || this.state.submitting || this.state.confirmationRequired || pending(status.analysis)
      || status.analysis?.state === "succeeded") return;
    const request = this.beginRequest();
    const retryOf = status.analysis?.analysis_id ?? null;
    this.update({ submitting: true, error: "", pollingPaused: false });
    try {
      const analysis = await this.client.submit(retryOf, request.signal);
      if (!request.current()) return;
      this.pollCount = 0;
      this.update({ status: { ...status, analysis }, submitting: false, confirmationRequired: false });
      this.pollLater();
    } catch (cause) {
      if (!request.current()) return;
      const uncertain = !(cause instanceof RegimexRequestError) || cause.status >= 500;
      this.update({ submitting: false, error: message(cause), confirmationRequired: uncertain });
      if (uncertain) {
        // A lost POST response can still mean an accepted job. Read before another submission.
        await this.loadStatus("Подтверждение запуска не получено. Проверьте состояние перед повторной отправкой.");
      }
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
