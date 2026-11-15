import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent } from "react";

import { alignedCharts, intervalGeometry, intervalKind, intervalLabels, priceDetail, priceLabel, stepLabel, stepTicks, stepX } from "./regimexChart";
import type { IntervalKind, PriceMode, StepRange } from "./regimexChart";
import type { PreviewInstrument } from "./regimexResult";

const PLOT_HEIGHT = 170, BAND_TOP = 200, BAND_HEIGHT = 16;
const kinds: IntervalKind[] = ["growth", "decline", "sideways", "no_trades", "insufficient_data"];
const priceStateLabels = {
  observed: "Цена сделки", carried: "Перенесённая цена",
  no_execution: "Нет цены: не было сделок", unknown: "Цена неизвестна",
};

export function RegimexPriceCharts({ instruments, range, mode }: {
  instruments: PreviewInstrument[]; range: StepRange; mode: PriceMode;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  const [activeStep, setActiveStep] = useState<number | null>(null);
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(180, Math.floor(entry.contentRect.width))));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const layout = useMemo(() => alignedCharts(instruments, range, width, PLOT_HEIGHT, mode), [instruments, range, width, mode]);
  return <div ref={container} className="regimex-charts" role="group" aria-label="Цены и режимы">
    {layout.series.map(({ instrument, chart }) => <PriceChart key={instrument.id} instrument={instrument}
      chart={chart} width={width} left={layout.left} plotWidth={layout.plotWidth} range={range} mode={mode}
      activeStep={activeStep} setActiveStep={setActiveStep} />)}
    <p className="regimex-chart-hint">Наведите на график или выберите его клавишей Tab. Стрелки переключают шаги.</p>
    {layout.series.some(({ chart }) => chart.points.some((point) => point.state === "carried"))
      && <p className="regimex-carried-key"><span aria-hidden="true" />Пунктир — перенесённая цена</p>}
  </div>;
}

function PriceChart({ instrument, chart, width, left, plotWidth, range, mode, activeStep, setActiveStep }: {
  instrument: PreviewInstrument; chart: ReturnType<typeof alignedCharts>["series"][number]["chart"];
  width: number; left: number; plotWidth: number; range: StepRange; mode: PriceMode;
  activeStep: number | null; setActiveStep: (step: number | null) => void;
}) {
  const id = useId();
  const active = chart.points.find((point) => point.step === activeStep);
  const activeInterval = active && instrument.intervals.find((interval) => interval.start <= active.step && interval.stop > active.step);
  const bands = useMemo(() => Object.fromEntries(kinds.map((kind) => [kind, instrument.intervals.filter((interval) => intervalKind(interval) === kind)
    .map((interval) => intervalGeometry(interval, range, plotWidth)).filter((box) => box !== null)
    .map((box) => `M${box.x},${BAND_TOP}h${box.width}v${BAND_HEIGHT}h${-box.width}Z`).join("")])), [instrument, range, plotWidth]);

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

  return <section className="regimex-chart" aria-label={`График ${instrument.baseAsset}/${instrument.quoteAsset}`}>
    <div className="regimex-chart-heading"><strong>{instrument.baseAsset}/{instrument.quoteAsset}</strong>
      <h3>{mode === "price" ? `Цена, ${instrument.quoteAsset}/${instrument.baseAsset}` : "Изменение цены, %"}</h3></div>
    {mode === "change" && <p className="regimex-baseline">{chart.baseline && chart.baseline.price > 0
      ? <>0% — {priceDetail(chart.baseline.price)} {instrument.quoteAsset}/{instrument.baseAsset} на шаге {chart.baseline.step + 1}
        {chart.baseline.state === "carried" && " (перенесённая цена)"}</>
      : "Базовая цена недоступна."}</p>}
    {chart.unavailableChange && <p className="form-error" role="alert">Процентное изменение не представимо для части цен.</p>}
    <svg width="100%" height="264" viewBox={`0 0 ${width} 264`} role="img" tabIndex={0}
      aria-labelledby={`${id}-title`} aria-describedby={`${id}-description ${id}-point`}
      onPointerMove={pointAtPointer} onPointerLeave={() => setActiveStep(null)}
      onFocus={() => setActiveStep(activeStep ?? chart.points[0]?.step ?? null)} onKeyDown={movePoint}>
      <title id={`${id}-title`}>{mode === "price" ? "Цена" : "Изменение цены"} и режимы {instrument.baseAsset}/{instrument.quoteAsset}, шаги {stepLabel(range)}</title>
      <desc id={`${id}-description`}>Стрелки влево и вправо выбирают общий шаг графиков. Пропуски разрывают линию; пунктир обозначает перенесённую цену.</desc>
      <defs><pattern id={`${id}-no-trades`} width="6" height="6" patternUnits="userSpaceOnUse">
        <rect width="6" height="6" fill="#d7dde3" /><path d="M0,6L6,0" stroke="#8995a2" strokeWidth="1" />
      </pattern></defs>
      <g transform={`translate(${left},12)`}>
        {chart.ticks.map((tick) => <g key={tick.value}>
          <line x1="0" x2={plotWidth} y1={tick.y} y2={tick.y} className="regimex-grid-line" />
          <text x="-9" y={tick.y + 4} textAnchor="end">{tick.label}</text>
        </g>)}
        {!chart.ticks.length && <text x={plotWidth / 2} y={PLOT_HEIGHT / 2} textAnchor="middle">Нет значений</text>}
        <path d={chart.paths.observed} className="regimex-price-line"><title>Цена сделки</title></path>
        <path d={chart.paths.carried} className="regimex-price-line regimex-carried-line"><title>Перенесённая цена</title></path>
        {kinds.map((kind) => <path key={kind} d={bands[kind]} className={`regimex-band regimex-${kind}`}
          style={kind === "no_trades" ? { fill: `url(#${id}-no-trades)` } : undefined} />)}
        {stepTicks(range, plotWidth).map((step) => <text key={step} x={stepX(step, range, plotWidth)}
          y={BAND_TOP + BAND_HEIGHT + 22} textAnchor="middle">{step + 1}</text>)}
        {active && <g className="regimex-cursor">
          <line x1={active.x} x2={active.x} y1="0" y2={BAND_TOP + BAND_HEIGHT} />
          {active.y !== null && <circle cx={active.x} cy={active.y} r="4" />}
        </g>}
      </g>
    </svg>
    <div id={`${id}-point`} className="regimex-point" aria-live="polite" aria-atomic="true">
      {active && <><strong>Шаг {active.step + 1}</strong>
        {mode === "change" && <span>{active.value === null ? "—" : `${priceLabel(active.value)}%`}</span>}
        <span>{active.price === null ? "—" : priceDetail(active.price)} {instrument.quoteAsset}/{instrument.baseAsset}</span>
        <span>{priceStateLabels[active.state]}</span>{activeInterval && <span>{intervalLabels[intervalKind(activeInterval)]}</span>}</>}
    </div>
  </section>;
}
