type Props = {
  label: string;
  page: number;
  pageSize: number;
  pageCount?: number;
  hasNext: boolean;
  back: () => void;
  next: () => void;
};

export function Pagination({ label, page, pageSize, pageCount, hasNext, back, next }: Props) {
  return <nav className="pagination" aria-label={label}>
    <button className="secondary-button" disabled={page === 1} onClick={back} type="button">Назад</button>
    <span className="user-meta">Страница {page}{pageCount ? ` из ${pageCount}` : ` · до ${pageSize} записей`}</span>
    <button className="secondary-button" disabled={!hasNext} onClick={next} type="button">Далее</button>
  </nav>;
}
