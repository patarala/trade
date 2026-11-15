import type { AnalysisPreview } from "./regimexResult";

export type PreviewInstrument = AnalysisPreview["instruments"][number];
export type PreviewInterval = PreviewInstrument["intervals"][number];
export type StepRange = { start: number; stop: number };
export type PriceMode = "price" | "change";
export type PriceDomain = { minimum: number; maximum: number };
export type IntervalKind = "growth" | "decline" | "sideways" | "no_trades" | "insufficient_data";

export const intervalLabels: Record<IntervalKind, string> = {
  growth: "↑ Рост",
  decline: "↓ Снижение",
  sideways: "→ Боковое",
  no_trades: "Нет сделок",
  insufficient_data: "Недостаточно данных",
};

export function intervalKind(interval: PreviewInterval): IntervalKind {
  return interval.status === "observed" ? interval.direction ?? "insufficient_data" : interval.status;
}

export function stepRange(steps: number[]): StepRange {
  return { start: steps[0] ?? 0, stop: steps.length ? steps[steps.length - 1] + 1 : 1 };
}

export function focusRange(interval: StepRange, full: StepRange): StepRange {
  const start = Math.max(full.start, interval.start);
  const stop = Math.min(full.stop, interval.stop);
  return start < stop ? { start, stop } : full;
}

export function stepLabel(range: StepRange): string {
  return range.stop - range.start === 1 ? String(range.start + 1) : `${range.start + 1}–${range.stop}`;
}

export function stepX(step: number, range: StepRange, width: number): number {
  return (step + 0.5 - range.start) / (range.stop - range.start) * width;
}

export function intervalGeometry(interval: StepRange, range: StepRange, width: number) {
  const start = Math.max(range.start, interval.start);
  const stop = Math.min(range.stop, interval.stop);
  if (stop <= start) return null;
  return {
    x: (start - range.start) / (range.stop - range.start) * width,
    width: (stop - start) / (range.stop - range.start) * width,
  };
}

export function stepTicks(range: StepRange, width: number): number[] {
  const count = Math.min(range.stop - range.start, Math.max(2, Math.floor(width / 80)));
  if (count === 1) return [range.start];
  return Array.from({ length: count }, (_, index) =>
    Math.round(range.start + index * (range.stop - range.start - 1) / (count - 1)));
}

export function priceBaseline(instrument: PreviewInstrument) {
  const index = instrument.prices.findIndex((price) => price !== null);
  return index < 0 ? null : {
    price: instrument.prices[index]!, step: instrument.steps[index], state: instrument.priceStates[index],
  };
}

function displayedPrice(price: number | null, baseline: ReturnType<typeof priceBaseline>, mode: PriceMode) {
  if (mode === "price" || price === null) return price;
  if (!baseline || baseline.price <= 0) return null;
  const value = (price - baseline.price) / baseline.price * 100;
  return Number.isFinite(value) ? value : null;
}

export function priceChart(instrument: PreviewInstrument, range: StepRange, width: number, height: number,
  mode: PriceMode = "price", domain?: PriceDomain) {
  // The reference belongs to the whole run, even while viewing a later interval.
  const baseline = priceBaseline(instrument);
  const samples = instrument.steps.flatMap((step, index) => step >= range.start && step < range.stop
    ? [{ step, price: instrument.prices[index], state: instrument.priceStates[index],
      value: displayedPrice(instrument.prices[index], baseline, mode) }] : []);
  const values = samples.flatMap((sample) => sample.value === null ? [] : [sample.value]);
  const minimum = domain?.minimum ?? (values.length ? Math.min(...values) : 0);
  const maximum = domain?.maximum ?? (values.length ? Math.max(...values) : 0);
  const y = (value: number) => minimum === maximum ? height / 2
    : height - 12 - (value - minimum) / (maximum - minimum) * (height - 24);
  const points = samples.map((sample) => ({ ...sample,
    x: stepX(sample.step, range, width), y: sample.value === null ? null : y(sample.value),
  }));
  const paths = { observed: "", carried: "" };
  let previous: typeof points[number] | null = null;
  let previousKind: keyof typeof paths | null = null;
  for (const point of points) {
    if (point.y === null) {
      previous = null;
      previousKind = null;
      continue;
    }
    const kind = point.state === "carried" ? "carried" : "observed";
    const position = `${point.x.toFixed(2)},${point.y.toFixed(2)}`;
    if (!previous || previous.step + 1 !== point.step) {
      paths[kind] += `M${position}h0`;
    } else {
      if (previousKind !== kind) paths[kind] += `M${previous.x.toFixed(2)},${previous.y!.toFixed(2)}`;
      paths[kind] += `L${position}`;
    }
    previous = point;
    previousKind = kind;
  }
  const ticks = !values.length ? [] : [...new Set([minimum, minimum + (maximum - minimum) / 2, maximum])];
  const labels = ticks.map(priceLabel);
  const compact = new Set(labels).size === ticks.length;
  const unavailableChange = mode === "change" && samples.some((sample) => sample.price !== null && sample.value === null);
  return { paths, points, baseline, unavailableChange, ticks: ticks.map((value, index) => ({ value, y: y(value),
    label: compact ? labels[index] : priceDetail(value),
  })) };
}

export function alignedCharts(instruments: PreviewInstrument[], range: StepRange, width: number, height: number,
  mode: PriceMode) {
  let domain: PriceDomain | undefined;
  if (mode === "change" && instruments.length > 1) {
    const values = instruments.flatMap((instrument) => {
      const baseline = priceBaseline(instrument);
      return instrument.steps.flatMap((step, index) => {
        const value = displayedPrice(instrument.prices[index], baseline, mode);
        return step >= range.start && step < range.stop && value !== null ? [value] : [];
      });
    });
    if (values.length) domain = { minimum: Math.min(...values), maximum: Math.max(...values) };
  }
  const axes = instruments.map((instrument) => priceChart(instrument, range, 1, height, mode, domain).ticks);
  const labelWidth = Math.max(0, ...axes.flatMap((ticks) => ticks.map((tick) => tick.label.length))) * 7 + 12;
  const left = Math.max(64, Math.min(width / 2, labelWidth));
  const plotWidth = Math.max(1, width - left - 12);
  return { left, plotWidth,
    series: instruments.map((instrument) => ({ instrument,
      chart: priceChart(instrument, range, plotWidth, height, mode, domain),
    })),
  };
}

const priceFormatter = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 6 });
const detailFormatter = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 21 });

export function priceDetail(value: number): string {
  return detailFormatter.format(value);
}

export function priceLabel(value: number): string {
  const absolute = Math.abs(value);
  return absolute >= 1e7 || (absolute > 0 && absolute < 0.001)
    ? value.toExponential(2) : priceFormatter.format(value);
}
