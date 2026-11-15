import type { PreviewInstrument, PreviewInterval } from "./regimexResult";
import { stepLabel } from "./regimexChart";
import "./regimexIntervalDetails.css";

const countFormatter = new Intl.NumberFormat();
const changeFormatter = new Intl.NumberFormat(undefined, {
  maximumSignificantDigits: 4, signDisplay: "exceptZero",
});
const shareFormatter = new Intl.NumberFormat(undefined, {
  style: "percent", maximumFractionDigits: 1,
});

export function RegimexIntervalDetails({ instrument, interval }: {
  instrument: PreviewInstrument; interval: PreviewInterval;
}) {
  const stats = interval.stats;
  const partialActivity = stats.activityObservedSteps < stats.stepCount;
  const partialPrices = stats.priceChangePercent !== null && stats.firstPriceStep !== null && stats.lastPriceStep !== null
    && (stats.firstPriceStep !== interval.start || stats.lastPriceStep !== interval.stop - 1);
  return <section className="regimex-interval-details" aria-label={`Подробности интервала ${stepLabel(interval)}`}>
    <h4>{instrument.baseAsset}/{instrument.quoteAsset} · шаги {stepLabel(interval)}</h4>
    <dl>
      <div>
        <dt>Изменение цены</dt>
        <dd>{stats.priceChangePercent === null ? "Нет данных" : `${changeFormatter.format(stats.priceChangePercent)}%`}</dd>
      </div>
      <div>
        <dt>Число сделок</dt>
        <dd>{stats.tradeCount === null
          ? stats.activityObservedSteps ? "Неполные данные" : "Нет данных"
          : countFormatter.format(stats.tradeCount)}</dd>
      </div>
      <div>
        <dt>Шаги без сделок{partialActivity ? " среди известных" : ""}</dt>
        <dd>{stats.inactiveStepShare === null ? "Нет данных" : <>
          {shareFormatter.format(stats.inactiveStepShare)}
          <span className="regimex-detail-count">{countFormatter.format(stats.inactiveSteps)} из {countFormatter.format(stats.activityObservedSteps)}</span>
        </>}</dd>
      </div>
    </dl>
    {partialPrices && <p>Изменение цены рассчитано по доступным ценам на шагах {stats.firstPriceStep! + 1} и {stats.lastPriceStep! + 1}.</p>}
    {partialActivity && <p>Активность известна для {countFormatter.format(stats.activityObservedSteps)} из {countFormatter.format(stats.stepCount)} шагов.</p>}
  </section>;
}
