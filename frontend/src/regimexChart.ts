import type { AnalysisPreview } from "./regimexResult";

export type PreviewInstrument = AnalysisPreview["instruments"][number];
export type PreviewInterval = PreviewInstrument["intervals"][number];
export type StepRange = { start: number; stop: number };
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

export function priceChart(instrument: PreviewInstrument, range: StepRange, width: number, height: number) {
  const samples = instrument.steps.flatMap((step, index) => step >= range.start && step < range.stop
    ? [{ step, price: instrument.prices[index], state: instrument.priceStates[index] }] : []);
  const values = samples.flatMap((sample) => sample.price === null ? [] : [sample.price]);
  const minimum = values.length ? Math.min(...values) : 0;
  const maximum = values.length ? Math.max(...values) : 0;
  const y = (value: number) => minimum === maximum ? height / 2
    : height - 12 - (value - minimum) / (maximum - minimum) * (height - 24);
  const points = samples.map((sample) => ({ ...sample,
    x: stepX(sample.step, range, width), y: sample.price === null ? null : y(sample.price),
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
  return { paths, points, ticks: ticks.map((value, index) => ({ value, y: y(value),
    label: compact ? labels[index] : priceDetail(value),
  })) };
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
