import { useEffect, useMemo, useState, useSyncExternalStore } from "react";

import { analysisStateLabels, RegimexApi } from "./regimexApi";
import { pending, RegimexController } from "./regimexController";
import { RegimexResult } from "./RegimexResult";
import { RegimexHistory } from "./RegimexHistory";
import { analysisLink } from "./regimexLink";

type Props = {
  simulationId: number;
  runId: number;
  token: string;
  requestedAnalysisId?: string;
  requestedInstrumentId?: string | null;
};

export function RegimexAnalysis({ simulationId, runId, token, requestedAnalysisId, requestedInstrumentId }: Props) {
  const api = useMemo(() => new RegimexApi(simulationId, runId, token),
    [simulationId, runId, token]);
  const controller = useMemo(() => new RegimexController(api, undefined, undefined, requestedAnalysisId),
    [api, requestedAnalysisId]);
  const [choice, setChoice] = useState<{ analysisId: string; instrumentId: string } | null>(null);
  const view = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  useEffect(() => {
    controller.start();
    return () => controller.stop();
  }, [controller]);

  const analysis = view.status?.analysis ?? null;
  const instrumentId = requestedInstrumentId ?? (choice?.analysisId === analysis?.analysis_id ? choice?.instrumentId : null)
    ?? view.preview?.instruments[0]?.id ?? null;
  const verifiedInstrumentId = view.preview?.instruments.some((item) => item.id === instrumentId) ? instrumentId : null;
  useEffect(() => {
    if (requestedAnalysisId && analysis && analysis.analysis_id !== requestedAnalysisId) {
      // An explicit retry creates a new resource; the address must follow that accepted analysis.
      window.location.hash = analysisLink({ simulationId, runId, analysisId: analysis.analysis_id,
        instrumentId: verifiedInstrumentId });
    }
  }, [requestedAnalysisId, analysis, simulationId, runId, verifiedInstrumentId]);

  if (!requestedAnalysisId && (view.status?.enabled === false || (!view.status && !view.error))) return null;
  const active = pending(analysis);
  const reportUnavailable = analysis?.state === "succeeded" && !analysis.report_available;
  const canSubmit = view.status?.eligible && !active && analysis?.state !== "succeeded";
  const busy = view.loading || view.submitting;
  const badge = analysis?.state === "succeeded" ? "completed"
    : analysis?.state === "failed" ? "failed" : analysis?.state === "cancelled" ? "stopped" : "running";

  function selectAnalysis(id: string) {
    window.location.hash = analysisLink({ simulationId, runId, analysisId: id, instrumentId: verifiedInstrumentId });
  }

  function selectInstrument(id: string) {
    if (!analysis || !view.preview?.instruments.some((item) => item.id === id)) return;
    setChoice({ analysisId: analysis.analysis_id, instrumentId: id });
    if (requestedAnalysisId) window.location.hash = analysisLink({ simulationId, runId,
      analysisId: analysis.analysis_id, instrumentId: id });
  }

  return <section className="panel regimex-panel" aria-label="Анализ Regimex">
    <div className="regimex-heading">
      <h2>Анализ Regimex</h2>
      {analysis && <span className={`run-status ${badge}`} role="status">
        {analysisStateLabels[analysis.state]}
      </span>}
    </div>

    {((view.status?.enabled && view.status.eligible) || (requestedAnalysisId && !view.status))
      && <RegimexHistory api={api} selected={analysis} select={selectAnalysis} />}
    {requestedAnalysisId && view.loading && <p className="panel-note" role="status">Открываем сохранённый анализ…</p>}
    {view.status?.enabled === false && requestedAnalysisId && <p className="form-error" role="alert">Regimex сейчас отключён. Сохранённый анализ недоступен.</p>}
    {view.status && !view.status.eligible && <p className="panel-note">{view.status.reason ?? "Этот запуск недоступен для анализа."}</p>}
    {analysis?.failure && <p className="form-error" role="alert">{analysis.failure.message}</p>}
    {view.error && <p className="form-error" role="alert">{view.error}</p>}
    {reportUnavailable && <p className="panel-note">Анализ завершён, но отчёт недоступен.</p>}
    {view.pollingPaused && active && !view.error && <p className="panel-note">Автообновление приостановлено. Проверьте состояние вручную.</p>}

    {view.previewLoading && <p className="panel-note" aria-live="polite">Загружаем классификацию…</p>}
    {view.previewError && <div className="form-error" role="alert">
      {view.previewError}{" "}
      <button className="text-button" type="button" disabled={view.previewLoading}
        onClick={() => { void controller.refreshPreview(); }}>Повторить загрузку</button>
    </div>}
    {analysis?.state === "succeeded" && view.preview?.analysisId === analysis.analysis_id
      && <RegimexResult key={view.preview.analysisId} preview={view.preview} instrumentId={instrumentId}
        onInstrumentChange={selectInstrument} />}

    {(view.status?.eligible || active || view.error || view.confirmationRequired) && <div className="card-actions regimex-actions">
      {canSubmit && !view.confirmationRequired && <button className="primary-button" disabled={busy}
        onClick={() => { void controller.submit(); }} type="button">
        {view.submitting ? "Отправляем…" : analysis ? "Повторить анализ" : "Анализировать"}
      </button>}
      {analysis?.state === "succeeded" && analysis.report_available && <button className="secondary-button"
        disabled={view.downloading !== null} onClick={() => { void controller.download("report"); }} type="button">
        {view.downloading === "report" ? "Скачиваем…" : "Скачать отчёт"}
      </button>}
      {(active || reportUnavailable || view.error || view.confirmationRequired || view.pollingPaused) && <button className="secondary-button"
        disabled={busy} onClick={() => { void controller.refresh(); }} type="button">
        {view.loading ? "Проверяем…" : "Проверить состояние"}
      </button>}
      {view.status?.eligible && <button className="text-button" disabled={view.downloading !== null}
        onClick={() => { void controller.download("export"); }} type="button">
        {view.downloading === "export" ? "Скачиваем…" : "Скачать данные"}
      </button>}
      {analysis && <a className="regimex-analysis-link" href={analysisLink({ simulationId, runId,
        analysisId: analysis.analysis_id, instrumentId: verifiedInstrumentId })}>Ссылка на анализ</a>}
    </div>}
  </section>;
}
