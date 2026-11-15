# Run Trade and Regimex locally

The optional local runner starts the browser interface, simulator API and workers,
Regimex API and workers, and a separate PostgreSQL cluster. It keeps this stack's
databases and credentials apart from other installations. All listeners use
`127.0.0.1`; this is a local development and demonstration environment.

## Requirements

- Linux or WSL, running as an ordinary user.
- Python 3.14.4 and uv 0.13.0.
- Node.js 24.22.3 with npm.
- PostgreSQL 16 binaries, including `postgres` and `initdb`.
- A trusted Regimex checkout and saved model bundle with its expected manifest
  SHA-256 digest. The runner does not train or download a model.

The first run installs locked Python and frontend dependencies, so it needs access
to their package registries. Docker is not required. The existing database-only
Compose configuration and ordinary simulator setup remain available separately.

## Start

From the Trade checkout, supply the paths and digest for your Regimex installation:

```sh
python3 -m backend.scripts.run_regimex \
  --regimex-checkout /path/to/regimex \
  --model-bundle /path/to/saved/model \
  --manifest-sha256 EXPECTED_MANIFEST_SHA256 \
  --demo mixed
```

If PostgreSQL tools are not on `PATH`, add `--postgres-bin /path/to/postgresql/bin`.
Use `--check` with the same arguments to validate prerequisites and the manifest
without starting services. `--demo` is optional; available examples are `growth`,
`decline`, `mixed` and `reversal`. They use constant simulator strategies and
deterministic seeds. Their names describe illustrative scenarios, not reference
labels or guaranteed predictions for every model.

Default addresses are:

| Component | Address |
| --- | --- |
| Trade interface and API | `http://127.0.0.1:18001` |
| Regimex API | `http://127.0.0.1:18002` |
| Private PostgreSQL cluster | `127.0.0.1:55445` |

Override them with `--trade-port`, `--regimex-port` and `--postgres-port`.
The runner uses Regimex's explicit loopback HTTP exception. For a remotely
accessible installation, use verified HTTPS and the service deployment guide;
changing these listeners to a public address is not a supported local-runner mode.

## State and credentials

State lives under `var/stack` by default. Use `--root /private/path` for a different
location. The directory is private, and credential files have mode `0600`.
`login.json` contains only the generated local UI account's email and password;
service secrets are kept separately and never printed by the runner. Open that
file locally to sign in, or register another account through the ordinary UI.

The selected demo configuration is created for the generated account. Select
**Запустить**, wait for completion, then use **Анализировать** in its results to
run the saved model. A completed analysis appears on the same
page and can be reopened using **Ссылка на анализ**. See the
[analysis guide](regimex.md#use-the-analysis).

The local runner creates its own database credentials and source-bound Regimex
connection. Generated credentials, observations, models copied into storage,
reports, dependency environments and logs stay under the private state directory;
they do not belong in Git or container build contexts.

## Stop and resume

Keep the runner in its terminal. Press **Ctrl+C** for an orderly shutdown of its
own services and database. Start it again with the same arguments and state
directory to resume using the saved data. A lock prevents two supervisors from
using the same state directory concurrently.

Inspect the supervisor state without printing credentials:

```sh
python3 -m backend.scripts.run_regimex --status --root var/stack
```

Run diagnostics using this stack's private connection configuration:

```sh
python3 -m backend.scripts.run_regimex --doctor --root var/stack
```

The check sends no observations and creates no analysis. See the
[integration guide](regimex.md#connection-diagnostics-and-capacity) for its scope.
Keep private state and credentials when preserving analyses; they are not disposable
build output. This local supervisor is not a replacement for a service manager,
remote deployment configuration or coordinated database/object backups.
