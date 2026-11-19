# Run Trade and Regimex locally

The local runner starts both applications, their workers and a private
PostgreSQL cluster on `127.0.0.1`. It creates a Trade account and registers
the saved model you supply.

## Requirements

Use Linux or WSL as a regular user. Install Node.js 24 with npm, PostgreSQL 16
and the uv version pinned in the Regimex checkout's `pyproject.toml` (0.13.0).
uv uses the Python version in that checkout's `.python-version`. Allow access
to the package registries on the first run to install the locked dependencies.

You need a trusted Regimex checkout, a saved model bundle and the expected
SHA-256 digest of the bundle's `manifest.json`.

## Start

Run this from the Trade checkout, replacing the paths and manifest digest.

```sh
python3 -m backend.scripts.run_regimex \
  --regimex-checkout /path/to/regimex \
  --model-bundle /path/to/saved/model \
  --manifest-sha256 EXPECTED_MANIFEST_SHA256 \
  --demo mixed
```

If `postgres` and `initdb` are outside your `PATH`, add
`--postgres-bin /path/to/postgresql/bin`. To check inputs, the manifest
digest and port availability before installing dependencies, add `--check`.

| Service | Default port | Override |
| --- | --- | --- |
| Trade interface and API | 18001 | `--trade-port` |
| Regimex API | 18002 | `--regimex-port` |
| PostgreSQL | 55445 | `--postgres-port` |

## Sign in and run the demo

1. Wait for the runner to print the Trade UI address, normally
   `http://127.0.0.1:18001`. Open it and sign in with the email and password
   from `var/stack/login.json`.
2. Open **Эксперименты**, find the imported configuration and choose
   **Запустить**. The `--demo` option imports a configuration for this account.
   It accepts `mixed`, `growth`, `decline` or `reversal`. Omit it to start
   without a demo. The saved model determines the directions you will see.
3. Open **Мониторинг и результаты**. Once the run finishes, click **Обновить**
   if needed, then **Анализировать**. The classification appears in the same
   panel. See the [chart and download controls](regimex.md#use-the-analysis).

## Stop and resume

Press **Ctrl+C** in the runner's terminal to stop its processes. The databases,
model files, reports and credentials stay in `var/stack`. Treat this directory
as private. Child process logs are in `var/stack/logs`.

To resume, start the runner with the same checkout, model, digest, PostgreSQL
binary directory and ports. An interrupted first start reuses the stack identity
and any credentials already saved. If it stopped after registering
the bundle, the next startup finishes its activation. An already active
model stays selected. To use different inputs, choose a new state
directory with `--root`. When using a custom root, look there for `login.json`
and logs.

Check whether the runner is active.

```sh
python3 -m backend.scripts.run_regimex --status --root var/stack
```

Check the running stack’s Regimex connection.

```sh
python3 -m backend.scripts.run_regimex --doctor --root var/stack
```
