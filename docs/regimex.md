# Regimex analysis

The simulator can export a completed run and ask a separate Regimex service to
classify its price history. The simulator continues to own users, experiments and
trading results. Regimex owns analysis jobs, saved-model inference and reports.
The connection is disabled by default and adds no database migration.

To start both applications and a private PostgreSQL cluster with one command, see
the [optional local runner](regimex-local.md). The instructions below connect an
ordinary simulator installation to an already running Regimex service.

## Enable the connection

In the Regimex administrator interface, open **Connections**, create a connection
with source namespace `trade`, and issue a key with `read`, `upload` and `analyze`
capabilities. Copy the key once into the simulator backend's private `.env`:

```dotenv
REGIMEX_URL=https://regimex.example.com
REGIMEX_TOKEN=replace_with_the_issued_connection_key
REGIMEX_SOURCE_ID=trade
```

Use the origin of your own Regimex service, without an API path. HTTPS certificates
are verified. The URL and key must be configured together; an incomplete or invalid
configuration stops backend startup. The key is never sent to the browser.
Leave the variables unset to hide the analysis controls and make no Regimex calls.

Use a different source namespace for each independent simulator installation and
configure the same namespace on its Regimex connection. Keep that namespace stable:
it identifies existing exports and analyses. To rotate a key, issue its replacement
on the same connection, update `.env`, restart the backend, verify access and revoke
the old key. Deletion and model-administration permissions are not required.

For local development with Regimex already listening on port 8000, use:

```dotenv
REGIMEX_URL=http://127.0.0.1:8000
REGIMEX_TOKEN=replace_with_the_issued_connection_key
REGIMEX_SOURCE_ID=trade
REGIMEX_ALLOW_LOOPBACK_HTTP=true
```

Run the simulator API on a separate port, with its ordinary environment active:

```sh
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8001
```

Run its simulation workers in another terminal:

```sh
python -m backend.scripts.run_simulation_workers --workers 2
```

Then start the frontend:

```sh
TRADE_API_ORIGIN=http://127.0.0.1:8001 npm --prefix frontend run dev
```

`TRADE_API_ORIGIN` configures Vite's development proxy and is read from its process
environment. Its default remains `http://localhost:8000`. The loopback HTTP exception
requires a numeric loopback address and must be unset for HTTPS deployments.

## Use the analysis

Open an experiment's run results. Once the run is complete, select **Анализировать**
in **Анализ Regimex**. When the job completes, its classification appears on the
page: select an instrument to see its price and direction intervals together.
Select an interval to inspect that part of the run, or reset the view to see the
whole history. Displayed step ranges use the simulator's numbering, starting at 1;
both displayed endpoints are included. Missing prices remain gaps, and intervals
without executions or sufficient observations stay distinct from direction labels.

The inline view uses the verified result JSON and observation export. It does not
load report HTML into the simulator page. **Повторить загрузку** retries a failed
preview request without creating a new analysis. **Скачать отчёт** still downloads
the full interactive HTML report, which works offline. **Скачать данные** downloads
the exact observation export.

Reloading the page retrieves the run's analysis from Regimex. Repeating a submission
after a lost response reuses the same job. A failed or cancelled job can be explicitly
retried with **Повторить анализ**; repeating that retry also reuses its accepted job.
An unavailable analysis service leaves the simulator's own results usable. After a
polling error, **Проверить состояние** resumes checking.

The **Анализ** selector lists saved analyses of the selected run, including their
model, status and time. **Новее** and **Старее** move through bounded history pages;
**Обновить историю** refreshes the list. Selecting an older analysis keeps its own
result and report available without submitting another job.

**Ссылка на анализ** opens a stable address for that analysis and selected
instrument. It restores the same result after a reload or sign-in and does not
contain credentials. The recipient still needs to own the run. An unavailable
analysis, invalid link or missing instrument produces an explicit error rather
than silently displaying a different result. This is analysis history for one
selected run; it does not add a simulator run-history screen.

Only complete runs of at most 4096 steps can be analyzed. Running, stopped, failed
and longer runs receive an explanation; they are never silently shortened. The
adapter currently supports simulator engine versions 2 and 3. A run must retain
its complete frozen instrument metadata.

## Data and access contract

Exports use Regimex observation schema 2.1. Native step `k` maps to row `k - 1` on
a model-time grid with origin 0 and width 1, in unit `simulation_step`. Every step
and instrument is present, including leading and trailing inactivity. A complete
native run of `N` steps covers `[0, N)`; these are simulation coordinates, not
elapsed wall-clock seconds.

Prices are the final execution prices within each step, initially using the run's
actual initial price and then carrying the last known price through inactive steps.
Each partial execution contributes one trade and its base-asset volume once.
Submission counts and volumes include accepted orders, including immediate fills
and orders cancelled at completion, and exclude rejected orders. Known inactivity
is zero. Prices and volumes are aggregated using decimal arithmetic; values that
cannot be represented faithfully in the JSON contract are rejected.

Historical bid/ask snapshots are not retained by this simulator. Both quote values
are therefore `null`, with states `unknown` and historical-quote capability
`unavailable`. The empty final order book is not used to invent earlier quotes.
Instrument symbols come from the frozen run configuration, so later catalogue edits
do not change an existing export. The export contains no strategy parameters, seeds
or user credentials.

The simulator backend checks the authenticated user's ownership of the explicitly
selected run before every analysis request and download. It supplies its own verified
subject and run identifiers to Regimex and verifies the returned source, run,
snapshot and content digest. Browser-supplied delegated identities are rejected.
Authentication continues to use the simulator's existing JWT and active-user check.

Outbound calls use one configured origin, verified TLS, bounded responses and an
overall deadline. Redirects are refused. Reports are downloaded as attachments,
never injected into the simulator page. Upstream errors are mapped to fixed public
messages without exposing service credentials or diagnostic payloads.

This integration establishes data and access compatibility. Prediction quality on
this simulator requires separate evaluation; generating a report does not establish
accuracy or turn model predictions into reference labels.

## Connection diagnostics and capacity

With the backend environment configured, run:

```sh
python -m backend.scripts.regimex_doctor
python -m backend.scripts.regimex_doctor --json
```

The command reads the same Regimex settings as the API; it does not require a
simulator database connection or JWT configuration. It checks the API, TLS when
enabled, the credential's source and capabilities, database readiness and active
model configuration. It uses a shared ten-second deadline and bounded responses.
The key is sent only to the configured connection endpoint after the API check;
health requests contain no credentials. No observations or analysis jobs are created.

Exit status is `0` for passed or explicitly disabled configuration and `1` for a
failed check. Worker execution is reported as untested; use Regimex's native
service diagnostics for worker health. A rejected key cannot be distinguished as
invalid, expired, revoked or attached to a paused connection from its `401` response.
Inspect its connection in Regimex rather than guessing the cause. Older Regimex
versions without `/api/v1/connection` receive an explicit diagnostic failure.

`REGIMEX_MAX_CONCURRENT_REQUESTS` defaults to `4` and accepts `1` through `16`.
The bound applies to all Regimex routes, including export downloads, before
database dependencies, export aggregation or shared synchronous worker threads.
Excess requests fail immediately with `503`, code `analysis_busy`, and
`Retry-After: 2`. Ordinary simulator routes bypass this budget. This is a
per-process capacity bound: multiple API processes each have their own allowance.
Regimex's separate connection and subject rate limits still apply upstream.

Adapter transport diagnostics use fixed error categories and durations. They do
not log credentials, request/response bodies, user identities or destination URLs.
The optional connection's availability does not change the simulator's `/health`.

## Release verification

The **Regimex integration** GitHub Actions workflow is an explicit manual check.
Open **Actions**, select that workflow, and choose **Run workflow** for the branch
being released. It checks out a reviewed Regimex commit, installs compatible locked
dependencies, starts a disposable PostgreSQL service and exercises real HTTP
export, saved-model inference, report download, request replay and access isolation.
Updating the supported Regimex revision is a deliberate workflow change.

The workflow creates a small artificial baseline bundle solely for protocol checks.
It does not download production models, publish artifacts, evaluate classification
accuracy or retrain the installed model. The ordinary push workflow stays separate
and inexpensive. For acceptance with a supplied model bundle, run the optional
integration command below with that bundle before release.

## API

All routes require the simulator user's bearer token and an explicit positive
`run_id` query parameter. Paths are relative to `/api/simulations/{simulation_id}`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/regimex` | Configuration availability, eligibility and latest analysis |
| GET | `/regimex/run` | Resolve one owned run from an analysis link |
| GET | `/regimex/export` | Download a completed schema-2.1 export |
| GET | `/regimex/analyses` | Saved analyses of this run, with `limit` (1–20) and `offset` (0–10000) |
| POST | `/regimex/analyses` | Submit `{}` or retry with `{"retry_of":"ANALYSIS_UUID"}` |
| GET | `/regimex/analyses/{analysis_id}` | Read the verified job state |
| GET | `/regimex/analyses/{analysis_id}/result` | Download the verified result JSON |
| GET | `/regimex/analyses/{analysis_id}/report` | Download the HTML report |

History returns `items`, `limit`, `offset` and `has_more`. Analysis views include
`model_version_id` and `created_at`. Every page retains the same owner/run/source
and snapshot digest checks as individual downloads; an invalid upstream entry
rejects the page rather than silently omitting that entry.

Export download also works without a configured Regimex URL or key. Configure
`REGIMEX_SOURCE_ID` to match the eventual receiving connection before exporting.

## Tests

With backend dependencies installed, run the ordinary suite without a live service:

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

`backend/requirements.lock` pins the tested backend dependencies. The source
requirements remain in `backend/requirements.txt`. Regenerate the lock deliberately
when updating dependencies, then rerun the checks. The fork's CI runs the backend
suite and frontend tests, lint and build for code changes; documentation-only pushes
do not trigger it. The real model integration test remains an explicit local check.

The optional real integration test additionally needs Regimex with its service,
scientific and reporting dependencies, a saved model bundle, and a disposable
PostgreSQL database. It creates isolated test schemas and runs both HTTP services
locally; it never connects to the installed production Regimex service. Use a test
database account that can create and remove schemas:

```sh
TRADE_REGIMEX_TEST_DSN=postgresql://test_user@127.0.0.1:5432/test_database \
TRADE_REGIMEX_TEST_MODEL=/absolute/path/to/saved/model/bundle \
DATABASE_URL=sqlite:// \
JWT_SECRET=integration-test-signing-key-32-bytes \
PASSWORD_PEPPER=integration-test-pepper \
python -m unittest backend.tests.test_regimex_integration
```

Without those opt-in settings, the integration test is explicitly skipped. It
checks real simulator output, saved-model inference, report download, repeated
submissions and access isolation. It is not a model-quality benchmark.
