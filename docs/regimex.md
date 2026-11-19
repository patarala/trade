# Regimex analysis

Regimex classifies price movements in a completed Trade run as growth, decline or
sideways. Open the classification in Trade or download an interactive HTML report
for offline use. To try it with a saved model on your machine, start with the
[local runner](regimex-local.md).

## Use the analysis

1. Open **Эксперименты** and choose **Мониторинг и результаты** on the
   experiment card. If the run has finished but the page still shows it
   running, click **Обновить**.
2. In **Анализ Regimex**, click **Анализировать**. Trade submits the run to
   Regimex's active model and checks the job until it finishes.
3. Under **Инструмент анализа**, choose a pair. Select an interval to zoom in and
   see its price change, total trades and share of steps without trades.
   **Весь запуск** restores the full range. For two instruments,
   **Все инструменты** shows both charts with the same cursor and zoom.
4. Use **Анализ** to reopen an earlier attempt for this run. Each entry shows the
   model, time and status. Bookmark **Ссылка на анализ** to reopen the same
   attempt and instrument when signed in as the run's owner.

**Нет сделок** and **Недостаточно данных** mark intervals without a predicted
direction. Chart steps start at 1. **Изменение, %** uses the first available
price in the whole run as its baseline, including when zoomed. The interval
details use the first and last available prices *inside the interval*. When an
endpoint price is missing, the details show which steps supplied those prices.
Price change needs two available prices, and the trade total needs a count for
every step. The inactive-step share uses steps with known counts as its denominator.

| Download | Contents |
| --- | --- |
| **Скачать отчёт** | The selected analysis as a standalone HTML report |
| **Скачать данные** | The run's JSON observation export for all instruments |
| **Интервалы CSV** | All saved intervals for the selected instrument, including intervals outside the current zoom |
| **Наблюдения CSV** | Observations for the selected instrument within the displayed range |

The CSV files include the analysis, snapshot, model and instrument identifiers.
A zero-based interval `[0, 10)` covers display steps 1 through 10. Empty numeric
cells mean unavailable values. The files use UTF-8, comma separators and decimal
points.

### When a request fails

| What happened | What to do |
| --- | --- |
| The analysis failed or was cancelled | Read the reason if shown. **Повторить анализ** requests a new attempt. |
| Trade reported that analysis requests are busy | Wait for the indicated delay and retry. Your selected attempt stays open. |
| Regimex storage is full | Ask the administrator to free space or raise the quota for the account or connection. |
| Regimex has no active model | Ask the Regimex administrator to publish a saved model, then retry the analysis. |
| The response to a launch request was lost | Click **Повторить запрос запуска**. Trade repeats the same request so Regimex can return the job it accepted. |
| Status updates paused | Click **Проверить состояние** to resume polling. |
| The job finished but its charts did not load | Click **Повторить загрузку** to reload the preview. The saved report can still be downloaded when available. |

## Enable the connection

For an existing installation, first publish or activate a saved model in Regimex.
Open **Connections** as a Regimex administrator and issue a key with `read`,
`upload` and `analyze` capabilities. Give each Trade installation its own stable
source namespace. Trade uses it to identify the installation's runs on Regimex.

Set the following in the Trade checkout's `.env`, using the origin and key for
your service and the namespace from the connection. Restart the Trade API.

```dotenv
REGIMEX_URL=https://regimex.example.com
REGIMEX_TOKEN=replace_with_the_issued_connection_key
REGIMEX_SOURCE_ID=trade
```

The connection is disabled when the URL and token are unset. Trade keeps the
key on the backend. To rotate it, issue another key on the same connection,
restart Trade with it, run the connection check below, then revoke the old key.

For local development with Regimex on port 8000, set
`REGIMEX_URL=http://127.0.0.1:8000` and `REGIMEX_ALLOW_LOOPBACK_HTTP=true`.
The HTTP option requires a numeric loopback address. Leave it unset for HTTPS.
After [setting up Trade](../README.md), start its three processes from the
checkout in separate terminals. Use the Trade Python environment for the first two.

```sh
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8001
```

```sh
python -m backend.scripts.run_simulation_workers --workers 2
```

```sh
TRADE_API_ORIGIN=http://127.0.0.1:8001 npm --prefix frontend run dev
```

## Connection diagnostics and capacity

Run the diagnostic in the Trade checkout with its backend environment active.

```sh
python -m backend.scripts.regimex_doctor
```

The command checks the configured origin, connection key, namespace, permissions,
analysis protocol, database readiness and active model. Trade requires
`regimex-analysis/1` in Regimex's `/api/v1/connection` response. When upgrading
the services independently, update Regimex first and confirm the diagnostic passes. If a key is rejected, inspect the
connection in Regimex. For worker health, run `regimex service doctor` in the
Regimex worker environment. For scripts, add `--json`. Exit status 0 means the
check passed or the integration is disabled. Failures return 1.

Each Trade API process accepts four simultaneous Regimex requests by default,
including exports. Set `REGIMEX_MAX_CONCURRENT_REQUESTS` to a value from 1 to 16
to change that limit. Exhausting it returns `503 analysis_busy` with
`Retry-After: 2` before the request reaches Regimex. A single analysis view
sends one request at a time. Other tabs and users share the backend limit.

## Data and access contract

The exporter accepts completed runs from engine versions 2 and 3, with up to
4096 steps and one or two instruments sharing a quote asset. It reads the
symbols and initial prices saved with the run. A run without its frozen
instrument metadata cannot be exported.

The schema-2.1 export contains one row per step and instrument. Simulator step
1 maps to export step 0. Each step has width 1 in `simulation_step` units.

| Field | Source |
| --- | --- |
| Last price | The last execution in the step. Before the first execution, Trade uses the initial price. Later inactive steps carry the previous price. |
| Trade count and volume | Each partial execution counts once. Volume is measured in the base asset. |
| Submitted buy and sell orders | Accepted orders, including immediate fills and orders later cancelled. Rejected orders are excluded. |
| Best bid and ask | Both are `null`, with state `unknown`. Trade does not store historical order-book snapshots. |

The browser authenticates to Trade. Trade checks ownership of the requested run,
sends the user's bound identity to Regimex and checks that each returned analysis
belongs to the same run and observation snapshot. The inline preview pairs the
saved result with a freshly built export and compares their digests. The direction
labels are predictions of the saved model. Measuring their accuracy on Trade
requires a separate evaluation.

Trade submits the export and an idempotency key together. Starting or retrying
an analysis takes one upstream request. Polling and history each take one
request and verify every returned snapshot digest against the local export,
whose identity can be cached for up to 60 seconds. A preview or download also
uses one request. Regimex returns the analysis, result and requested HTML
together so Trade can check their identifiers, model, time grid and digest
before serving them. The local JSON export is available when Regimex is offline
or disabled.
If completed events are edited directly in the database, restart the API before
reading their analysis again. The simulator normally treats these runs as immutable.

## API

Routes are relative to `/api/simulations/{simulation_id}`. Each requires the
Trade user's bearer token and a positive `run_id` query parameter.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/regimex` | Connection availability, run eligibility and latest analysis |
| GET | `/regimex/run` | Resolve the run in a saved analysis link |
| GET | `/regimex/export` | Download observations, including when the Regimex connection is disabled |
| GET | `/regimex/analyses` | List saved analyses. The response includes `items`, `limit` (1–20), `offset` (0–10000) and `has_more`. |
| POST | `/regimex/analyses` | Submit `{}` or retry with `{"retry_of":"ANALYSIS_UUID"}` |
| GET | `/regimex/analyses/{analysis_id}` | Read job state and model version |
| GET | `/regimex/analyses/{analysis_id}/preview` | Read the result and observations together, up to 16 MiB |
| GET | `/regimex/analyses/{analysis_id}/result` | Download result JSON |
| GET | `/regimex/analyses/{analysis_id}/report` | Download the HTML report |

## Tests

From the Trade checkout with the backend dependencies installed, run the backend
suite and frontend checks.

```sh
DATABASE_URL=sqlite:// \
JWT_SECRET=integration-test-signing-key-32-bytes \
PASSWORD_PEPPER=integration-test-pepper \
python -m unittest discover -s backend/tests -t .
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
```

For browser controls, install Chromium and run the Playwright suite against its
HTTP fixtures. On Linux hosts missing system libraries, add `--with-deps` to
the install command.

```sh
(cd frontend && npx playwright install chromium)
npm --prefix frontend run test:browser
```

To exercise both services over HTTP, run **Regimex integration** from GitHub
Actions. It uses the Regimex revision pinned in the workflow, PostgreSQL and a
small saved model. To check a particular model locally, install Trade's backend
dependencies and Regimex with its `service`, `ml` and `viz` extras in the test
environment. Supply a disposable PostgreSQL database where the test account
can create and remove schemas.

```sh
TRADE_REGIMEX_TEST_DSN=postgresql://test_user@127.0.0.1:5432/test_database \
TRADE_REGIMEX_TEST_MODEL=/absolute/path/to/saved/model/bundle \
DATABASE_URL=sqlite:// \
JWT_SECRET=integration-test-signing-key-32-bytes \
PASSWORD_PEPPER=integration-test-pepper \
python -m unittest backend.tests.test_regimex_integration
```
