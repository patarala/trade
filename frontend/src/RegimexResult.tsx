import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent } from "react";

import type { AnalysisPreview } from "./regimexResult";
import {
  focusRange, intervalGeometry, intervalKind, intervalLabels, priceChart, priceDetail,
  stepLabel, stepRange, stepTicks, stepX,
} from "./regimexChart";
import type { IntervalKind, PreviewInstrument, StepRange } from "./regimexChart";
import "./regimexResult.css";

const PAGE_SIZE = 20;
const kinds: IntervalKind[] = ["growth", "decline", "sideways", "no_trades", "insufficient_data"];
const priceStateLabels = {
  observed: "Цена сделки", carried: "Перенесённая цена",
  no_execution: "Нет цены: не было сделок", unknown: "Цена неизвестна",
};

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
    {instrument ? <InstrumentResult key={instrument.id} instrument={instrument} />
      : <p className="form-error" role="alert">Инструмента из ссылки нет в этом анализе. Выберите доступный инструмент.</p>}
  </section>;
}

function InstrumentResult({ instrument }: { instrument: PreviewInstrument }) {
  const fullRange = stepRange(instrument.steps);
  const [focus, setFocus] = useState<StepRange | null>(null);
  const [page, setPage] = useState(0);
  const range = focus ? focusRange(focus, fullRange) : fullRange;
  const zoomed = range.start !== fullRange.start || range.stop !== fullRange.stop;
  const pageCount = Math.max(1, Math.ceil(instrument.intervals.length / PAGE_SIZE));
  const visibleKinds = kinds.filter((kind) => kind === "growth" || kind === "decline" || kind === "sideways"
    || instrument.intervals.some((interval) => intervalKind(interval) === kind));
  return <>
    <div className="regimex-chart-heading">
      <h3>Цена, {instrument.quoteAsset}/{instrument.baseAsset}</h3>
      <div><span>Шаги {stepLabel(range)}</span>{zoomed && <button className="text-button" type="button"
        onClick={() => setFocus(null)}>Весь запуск</button>}</div>
    </div>
    <PriceChart key={`${range.start}:${range.stop}`} instrument={instrument} range={range} />
    <ul className="regimex-legend" aria-label="Обозначения режимов">
      {visibleKinds.map((kind) => <li key={kind}><span className={`regimex-swatch regimex-${kind}`} aria-hidden="true" />{intervalLabels[kind]}</li>)}
    </ul>
    <div className="regimex-interval-heading"><h4>Интервалы</h4>
      <span>{instrument.intervals.length}</span></div>
    <div className="regimex-interval-table"><table>
      <caption className="regimex-sr-only">Интервалы классификации. Выберите шаги, чтобы приблизить график.</caption>
      <thead><tr><th scope="col">Шаги</th><th scope="col">Режим</th></tr></thead>
      <tbody>{instrument.intervals.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map((interval) => {
        const kind = intervalKind(interval);
        const selected = focus?.start === interval.start && focus.stop === interval.stop;
        return <tr key={`${interval.start}:${interval.stop}`} className={selected ? "regimex-selected" : ""}>
          <td><button className="regimex-interval-button" type="button" aria-pressed={selected}
            aria-label={`Показать шаги ${stepLabel(interval)}: ${intervalLabels[kind]}`}
            onClick={() => setFocus(selected ? null : interval)}>{stepLabel(interval)} <span aria-hidden="true">↗</span></button></td>
          <td><span className={`regimex-swatch regimex-${kind}`} aria-hidden="true" />{intervalLabels[kind]}</td>
        </tr>;
      })}</tbody>
    </table></div>
    {pageCount > 1 && <nav className="regimex-pagination" aria-label="Страницы интервалов">
      <button className="secondary-button" disabled={page === 0} onClick={() => setPage(page - 1)} type="button">Назад</button>
      <span>{page + 1} / {pageCount}</span>
      <button className="secondary-button" disabled={page + 1 >= pageCount} onClick={() => setPage(page + 1)} type="button">Далее</button>
    </nav>}
  </>;
}

function PriceChart({ instrument, range }: { instrument: PreviewInstrument; range: StepRange }) {
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  const [activeStep, setActiveStep] = useState<number | null>(null);
  const id = useId();
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(180, Math.floor(entry.contentRect.width))));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const top = 12, plotHeight = 170, bandTop = 200, bandHeight = 16;
  const axis = useMemo(() => priceChart(instrument, range, 1, plotHeight).ticks, [instrument, range]);
  const labelWidth = Math.max(0, ...axis.map((tick) => tick.label.length)) * 7 + 12;
  const left = Math.max(64, Math.min(width / 2, labelWidth));
  const plotWidth = Math.max(1, width - left - 12);
  const chart = useMemo(() => priceChart(instrument, range, plotWidth, plotHeight), [instrument, range, plotWidth]);
  const active = chart.points.find((point) => point.step === activeStep);
  const activeInterval = active && instrument.intervals.find((interval) => interval.start <= active.step && interval.stop > active.step);
  const bands = useMemo(() => Object.fromEntries(kinds.map((kind) => [kind, instrument.intervals.filter((interval) => intervalKind(interval) === kind)
    .map((interval) => intervalGeometry(interval, range, plotWidth)).filter((box) => box !== null)
    .map((box) => `M${box.x},${bandTop}h${box.width}v${bandHeight}h${-box.width}Z`).join("")])), [instrument, range, plotWidth]);

  function pointAtPointer(event: PointerEvent<SVGSVGElement>) {
    const bounds = event.currentTarget.getBoundingClientRect();
    const x = (event.clientX - bounds.left) * width / bounds.width - left;
    const step = Math.round(range.start - 0.5 + x / plotWidth * (range.stop - range.start));
    setActiveStep(Math.max(range.start, Math.min(range.stop - 1, step)));
  }

  function movePoint(event: KeyboardEvent<SVGSVGElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = chart.points.findIndex((point) => point.step === activeStep);
    const index = event.key === "Home" ? 0 : event.key === "End" ? chart.points.length - 1
      : Math.max(0, Math.min(chart.points.length - 1, current + (event.key === "ArrowRight" ? 1 : -1)));
    setActiveStep(chart.points[index]?.step ?? null);
  }

  return <div ref={container} className="regimex-chart">
    <svg width="100%" height="264" viewBox={`0 0 ${width} 264`} role="img" tabIndex={0}
      aria-labelledby={`${id}-title`} aria-describedby={`${id}-description ${id}-point`}
      onPointerMove={pointAtPointer} onPointerLeave={() => setActiveStep(null)}
      onFocus={() => setActiveStep(chart.points[0]?.step ?? null)} onBlur={() => setActiveStep(null)} onKeyDown={movePoint}>
      <title id={`${id}-title`}>Цена и режимы {instrument.baseAsset}/{instrument.quoteAsset}, шаги {stepLabel(range)}</title>
      <desc id={`${id}-description`}>Стрелки влево и вправо выбирают шаг. Пропуски цены разрывают линию; пунктир обозначает перенесённую цену.</desc>
      <defs><pattern id={`${id}-no-trades`} width="6" height="6" patternUnits="userSpaceOnUse">
        <rect width="6" height="6" fill="#d7dde3" /><path d="M0,6L6,0" stroke="#8995a2" strokeWidth="1" />
      </pattern></defs>
      <g transform={`translate(${left},${top})`}>
        {chart.ticks.map((tick) => <g key={tick.value}>
          <line x1="0" x2={plotWidth} y1={tick.y} y2={tick.y} className="regimex-grid-line" />
          <text x="-9" y={tick.y + 4} textAnchor="end">{tick.label}</text>
        </g>)}
        {!chart.ticks.length && <text x={plotWidth / 2} y={plotHeight / 2} textAnchor="middle">Нет цен</text>}
        <path d={chart.paths.observed} className="regimex-price-line"><title>Цена сделки</title></path>
        <path d={chart.paths.carried} className="regimex-price-line regimex-carried-line"><title>Перенесённая цена</title></path>
        {kinds.map((kind) => <path key={kind} d={bands[kind]} className={`regimex-band regimex-${kind}`}
          style={kind === "no_trades" ? { fill: `url(#${id}-no-trades)` } : undefined} />)}
        {stepTicks(range, plotWidth).map((step) => <text key={step} x={stepX(step, range, plotWidth)}
          y={bandTop + bandHeight + 22} textAnchor="middle">{step + 1}</text>)}
        {active && <g className="regimex-cursor">
          <line x1={active.x} x2={active.x} y1="0" y2={bandTop + bandHeight} />
          {active.y !== null && <circle cx={active.x} cy={active.y} r="4" />}
        </g>}
      </g>
    </svg>
    <div id={`${id}-point`} className="regimex-point" aria-live="polite" aria-atomic="true">
      {active ? <><strong>Шаг {active.step + 1}</strong><span>{active.price === null ? "—" : priceDetail(active.price)} {instrument.quoteAsset}/{instrument.baseAsset}</span>
        <span>{priceStateLabels[active.state]}</span>{activeInterval && <span>{intervalLabels[intervalKind(activeInterval)]}</span>}</>
        : <span>Наведите на график или выберите его клавишей Tab.</span>}
    </div>
    {chart.points.some((point) => point.state === "carried") && <p className="regimex-carried-key"><span aria-hidden="true" />Пунктир — перенесённая цена</p>}
  </div>;
}
