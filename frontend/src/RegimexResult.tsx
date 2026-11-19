import { Fragment, useMemo, useState } from "react";

import type { AnalysisPreview, PreviewInstrument } from "./regimexResult";
import { focusRange, intervalKind, intervalLabels, stepLabel, stepRange } from "./regimexChart";
import type { IntervalKind, PriceMode, StepRange } from "./regimexChart";
import { intervalsCsv, observationsCsv } from "./regimexCsv";
import { saveDownload } from "./regimexApi";
import { Pagination } from "./Pagination";
import { RegimexIntervalDetails } from "./RegimexIntervalDetails";
import { RegimexPriceCharts } from "./RegimexPriceCharts";
import "./regimexResult.css";

const PAGE_SIZE = 20;
const kinds: IntervalKind[] = ["growth", "decline", "sideways", "no_trades", "insufficient_data"];

export function RegimexResult({ preview, instrumentId, onInstrumentChange }: {
  preview: AnalysisPreview; instrumentId: string | null; onInstrumentChange: (id: string) => void;
}) {
  const instrument = preview.instruments.find((item) => item.id === instrumentId);
  return <section className="regimex-result" aria-label="Классификация модели">
    <div className="regimex-result-heading">
      <p className="regimex-model">Предсказание модели · {preview.modelVersion}</p>
      <label>Инструмент анализа
        <select aria-label="Инструмент анализа" value={instrument?.id ?? ""} onChange={(event) => onInstrumentChange(event.target.value)}>
          {!instrument && <option value="" disabled>Инструмент не найден</option>}
          {preview.instruments.map((item) => <option key={item.id} value={item.id}>{item.baseAsset}/{item.quoteAsset}</option>)}
        </select>
      </label>
    </div>
    {instrument ? <Exploration preview={preview} instrument={instrument} />
      : <p className="form-error" role="alert">Инструмента из ссылки нет в этом анализе. Выберите доступный инструмент.</p>}
  </section>;
}

function Exploration({ preview, instrument }: { preview: AnalysisPreview; instrument: PreviewInstrument }) {
  const [allInstruments, setAllInstruments] = useState(false);
  const [mode, setMode] = useState<PriceMode>("price");
  const [focus, setFocus] = useState<StepRange | null>(null);
  const [downloadError, setDownloadError] = useState("");
  const fullRange = useMemo(() => stepRange(instrument.steps), [instrument]);
  const range = useMemo(() => focus ? focusRange(focus, fullRange) : fullRange, [focus, fullRange]);
  const shown = useMemo(() => allInstruments ? preview.instruments : [instrument], [allInstruments, preview.instruments, instrument]);
  const visibleKinds = kinds.filter((kind) => ["growth", "decline", "sideways"].includes(kind)
    || shown.some((item) => item.intervals.some((interval) => intervalKind(interval) === kind)));

  function download(kind: "intervals" | "observations") {
    setDownloadError("");
    try {
      const content = kind === "intervals" ? intervalsCsv(preview, instrument.id)
        : observationsCsv(preview, instrument.id, range);
      const suffix = kind === "intervals" ? "intervals" : `steps-${range.start + 1}-${range.stop}`;
      const instrumentIndex = preview.instruments.indexOf(instrument) + 1;
      saveDownload(new Blob([content], { type: "text/csv;charset=utf-8" }),
        `regimex-${preview.analysisId}-instrument-${instrumentIndex}-${suffix}.csv`);
    } catch {
      setDownloadError("Не удалось подготовить CSV. Повторите скачивание.");
    }
  }

  return <>
    <div className="regimex-exploration-controls">
      <label>Отображение цены
        <select aria-label="Отображение цены" value={mode} onChange={(event) => setMode(event.target.value as PriceMode)}>
          <option value="price">Цена</option><option value="change">Изменение, %</option>
        </select>
      </label>
      {preview.instruments.length > 1 && <label className="regimex-all-instruments">
        <input type="checkbox" checked={allInstruments} onChange={(event) => setAllInstruments(event.target.checked)} />Все инструменты
      </label>}
      <div className="regimex-shared-range"><span>Шаги {stepLabel(range)}</span>
        {focus && <button className="text-button" type="button" onClick={() => setFocus(null)}>Весь запуск</button>}
      </div>
    </div>
    <RegimexPriceCharts key={`${range.start}:${range.stop}`} instruments={shown} range={range} mode={mode} />
    <ul className="regimex-legend" aria-label="Обозначения режимов">
      {visibleKinds.map((kind) => <li key={kind}><span className={`regimex-swatch regimex-${kind}`} aria-hidden="true" />{intervalLabels[kind]}</li>)}
    </ul>
    <IntervalTable key={instrument.id} instrument={instrument} focus={focus} select={setFocus} />
    <div className="regimex-csv-actions" aria-label={`Экспорт ${instrument.baseAsset}/${instrument.quoteAsset}`}>
      <button className="secondary-button" type="button" onClick={() => download("intervals")}
        title={`Все интервалы ${instrument.baseAsset}/${instrument.quoteAsset}`}>Интервалы CSV</button>
      <button className="secondary-button" type="button" onClick={() => download("observations")}
        title={`${instrument.baseAsset}/${instrument.quoteAsset}, шаги ${stepLabel(range)}`}>Наблюдения CSV</button>
      <span>{instrument.baseAsset}/{instrument.quoteAsset} · наблюдения: шаги {stepLabel(range)}</span>
    </div>
    {downloadError && <p className="form-error" role="alert">{downloadError}</p>}
  </>;
}

function IntervalTable({ instrument, focus, select }: {
  instrument: PreviewInstrument; focus: StepRange | null; select: (range: StepRange | null) => void;
}) {
  const [page, setPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil(instrument.intervals.length / PAGE_SIZE));
  return <>
    <div className="regimex-interval-heading"><h4>Интервалы {instrument.baseAsset}/{instrument.quoteAsset}</h4>
      <span>{instrument.intervals.length}</span></div>
    <div className="regimex-interval-table"><table>
      <caption className="regimex-sr-only">Интервалы классификации. Выберите шаги, чтобы приблизить графики и открыть подробности участка.</caption>
      <thead><tr><th scope="col">Шаги</th><th scope="col">Режим</th></tr></thead>
      <tbody>{instrument.intervals.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map((interval) => {
        const kind = intervalKind(interval);
        const selected = focus?.start === interval.start && focus.stop === interval.stop;
        return <Fragment key={`${interval.start}:${interval.stop}`}>
          <tr className={selected ? "regimex-selected" : ""}>
            <td><button className="regimex-interval-button" type="button" aria-pressed={selected}
              aria-label={`Показать шаги ${stepLabel(interval)}: ${intervalLabels[kind]}`}
              onClick={() => select(selected ? null : interval)}>{stepLabel(interval)} <span aria-hidden="true">↗</span></button></td>
            <td><span className={`regimex-swatch regimex-${kind}`} aria-hidden="true" />{intervalLabels[kind]}</td>
          </tr>
          {selected && <tr className="regimex-detail-row"><td colSpan={2}>
            <RegimexIntervalDetails instrument={instrument} interval={interval} />
          </td></tr>}
        </Fragment>;
      })}</tbody>
    </table></div>
    {pageCount > 1 && <Pagination label="Страницы интервалов" page={page + 1} pageSize={PAGE_SIZE} pageCount={pageCount}
      hasNext={page + 1 < pageCount} back={() => setPage(page - 1)} next={() => setPage(page + 1)} />}
  </>;
}
