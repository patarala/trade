import { useCallback, useState } from "react";

import type { Analysis, RegimexApi } from "./regimexApi";
import { analysisStateLabels } from "./regimexApi";
import { Pagination } from "./Pagination";
import { useRequest } from "./useRequest";

type Props = { api: RegimexApi; selected: Analysis | null; select: (id: string) => void };

export function RegimexHistory(props: Props) {
  const [offset, setOffset] = useState(0);
  return <HistoryPage key={offset} {...props} offset={offset} move={setOffset} />;
}

function label(item: Analysis): string {
  const date = new Date(item.created_at).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "medium" });
  return `${item.model_version_id} · ${analysisStateLabels[item.state]} · ${date}`;
}

function HistoryPage({ api, selected, select, offset, move }: Props & { offset: number; move: (offset: number) => void }) {
  const id = selected?.analysis_id;
  const load = useCallback((signal: AbortSignal) => api.history(offset, signal), [api, offset]);
  const { data, error, refresh } = useRequest(load);
  const items = data?.items.map((item) => item.analysis_id === id ? selected! : item) ?? [];
  if (selected && !items.some((item) => item.analysis_id === id)) items.unshift(selected);
  return <div className="regimex-history">
    {error ? <div className="form-error" role="alert">{error}{" "}
      <button className="text-button" onClick={refresh} type="button">Обновить историю</button>
    </div> : !data && <p className="panel-note" role="status">Загружаем историю анализов…</p>}
    {data && <>
      {items.length > 0 ? <label>Анализ
        <select aria-label="Анализ" value={id ?? ""} onChange={(event) => select(event.target.value)}>
          {!id && <option value="" disabled>Выберите анализ</option>}
          {items.map((item) => <option key={item.analysis_id} value={item.analysis_id}>{label(item)}</option>)}
        </select>
      </label> : <p className="panel-note">Сохранённых анализов пока нет.</p>}
      {!error && <button className="text-button regimex-refresh-history" onClick={refresh} type="button">Обновить историю</button>}
      {(offset > 0 || data.has_more) && <Pagination label="История анализов"
        page={1 + Math.floor(offset / data.limit)} pageSize={data.limit}
        back={() => move(Math.max(0, offset - data.limit))}
        next={() => move(offset + data.limit)} hasNext={data.has_more && offset + data.limit <= 10000} />}
      {data.has_more && offset + data.limit > 10000 && <p className="panel-note">Достигнут предел просмотра истории. Более старый анализ можно открыть по сохранённой ссылке.</p>}
    </>}
  </div>;
}
