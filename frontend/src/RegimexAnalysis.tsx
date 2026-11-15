import { useEffect, useMemo, useSyncExternalStore } from "react";

import { RegimexApi } from "./regimexApi";
import type { AnalysisState } from "./regimexApi";
import { pending, RegimexController } from "./regimexController";

const stateLabels: Record<AnalysisState, string> = {
  queued: "В очереди",
  running: "Выполняется",
  cancel_requested: "Останавливается",
  succeeded: "Готов",
  failed: "Ошибка анализа",
  cancelled: "Отменён",
};

type Props = {
  simulationId: number;
  runId: number;
  token: string;
};

export function RegimexAnalysis({ simulationId, runId, token }: Props) {
  const controller = useMemo(() => new RegimexController(new RegimexApi(simulationId, runId, token)),
    [simulationId, runId, token]);
  const view = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  useEffect(() => {
    controller.start();
    return () => controller.stop();
  }, [controller]);

  if (view.status?.enabled === false || (!view.status && !view.error)) return null;
  const analysis = view.status?.analysis ?? null;
  const active = pending(analysis);
  const reportUnavailable = analysis?.state === "succeeded" && !analysis.report_available;
  const canSubmit = view.status?.eligible && !active && analysis?.state !== "succeeded";
  const busy = view.loading || view.submitting;
  const badge = analysis?.state === "succeeded" ? "completed"
    : analysis?.state === "failed" ? "failed" : analysis?.state === "cancelled" ? "stopped" : "running";

  return <section className="panel regimex-panel" aria-label="Анализ Regimex">
    <div className="regimex-heading">
      <div><h2>Анализ Regimex</h2>
        <p className="panel-note">Рыночные режимы по результатам запуска.</p>
      </div>
      {analysis && <span className={`run-status ${badge}`} role="status">
        {stateLabels[analysis.state]}
      </span>}
    </div>

    {view.status && !view.status.eligible && <p className="panel-note">{view.status.reason ?? "Этот запуск недоступен для анализа."}</p>}
    {analysis?.failure && <p className="form-error" role="alert">{analysis.failure.message}</p>}
    {view.error && <p className="form-error" role="alert">{view.error}</p>}
    {reportUnavailable && <p className="panel-note">Анализ завершён, но отчёт недоступен.</p>}
    {view.pollingPaused && active && !view.error && <p className="panel-note">Автообновление приостановлено. Проверьте состояние вручную.</p>}

    {(view.status?.eligible || active || view.error || view.confirmationRequired) && <div className="card-actions regimex-actions">
      {canSubmit && !view.confirmationRequired && <button className="primary-button" disabled={busy}
        onClick={() => { void controller.submit(); }} type="button">
        {view.submitting ? "Отправляем…" : analysis ? "Повторить анализ" : "Анализировать"}
      </button>}
      {analysis?.state === "succeeded" && analysis.report_available && <button className="primary-button"
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
    </div>}
  </section>;
}
