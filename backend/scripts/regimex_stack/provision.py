"""Bootstrap only this runner's databases, existing model and local account.

Executed by the appropriate isolated environment; optional Regimex imports never
enter the ordinary simulator runtime.
"""

import json
import sys
import time
from pathlib import Path

from .files import read_private, save_json, write_private
from .processes import local_request


def database(root, state, credentials):
    import psycopg
    from psycopg import sql

    deadline = time.monotonic() + 30
    while True:
        try:
            connection = psycopg.connect(
                host="127.0.0.1",
                port=state["postgres_port"],
                dbname="postgres",
                user="stack_admin",
                password=credentials["database_admin"],
                autocommit=True,
                connect_timeout=2,
            )
            break
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Owned PostgreSQL did not become ready.") from None
            time.sleep(0.2)
    with connection:
        for name in ("trade", "regimex"):
            role = "stack_" + name
            if not connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
            ).fetchone():
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE"
                    ).format(
                        sql.Identifier(role),
                        sql.Literal(credentials["database_" + name]),
                    )
                )
            if not connection.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (name,)
            ).fetchone():
                connection.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(name), sql.Identifier(role)
                    )
                )
            connection.execute(
                sql.SQL("REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(name)
                )
            )
            connection.execute(
                sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
                    sql.Identifier(name), sql.Identifier(role)
                )
            )
    # Application roles own their own schemas, never each other's database.
    for name in ("trade", "regimex"):
        with psycopg.connect(
            host="127.0.0.1",
            port=state["postgres_port"],
            dbname=name,
            user="stack_" + name,
            password=credentials["database_" + name],
            connect_timeout=5,
        ) as own:
            own.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")


def regimex(root, state, _credentials):
    from regimex.model_registry import InferenceRuntime
    from regimex.service_bootstrap import create_service
    from regimex.service_contracts import NotFound
    from regimex.service_settings import ServiceSettings

    service = create_service(ServiceSettings.from_env())
    service.repository.migrate()
    admin_path = root / "admin.token"
    if not admin_path.exists():
        principal, token = service.repository.issue_token("local-stack-admin", "admin")
        try:
            write_private(admin_path, token + "\n")
        except BaseException:
            service.repository.revoke_token(principal.token_id)
            raise
    admin = service.authenticate(read_private(admin_path).strip())
    manifest = json.loads((Path(state["model_bundle"]) / "manifest.json").read_text())
    try:
        existing = service.repository.get_model(manifest["model_version_id"])
    except NotFound:
        existing = None
    # Registration verifies every bundle member and restores its saved predictor.
    # Its native existing-version path reuses the immutable model on restart.
    service.register_bundle(
        admin,
        Path(state["model_bundle"]),
        manifest_sha256=state["manifest_sha256"],
        inference_runtime=InferenceRuntime(threads=1),
        activate=existing is None and not (root / "model-ready.json").exists(),
    )
    save_json(root / "model-ready.json", {"manifest_sha256": state["manifest_sha256"]})
    integration_path = root / "integration.token"
    if not integration_path.exists():
        principal, token = service.repository.issue_token(
            "trade-local-stack",
            "integration",
            source_namespace=state["source_id"],
        )
        try:
            write_private(integration_path, token + "\n")
        except BaseException:
            service.repository.revoke_token(principal.token_id)
            raise
    principal = service.authenticate(read_private(integration_path).strip())
    if principal.role != "integration" or principal.source_namespace != state["source_id"]:
        raise ValueError("Saved integration credential does not belong to this stack.")
    if not {"read", "upload", "analyze"}.issubset(principal.integration_scopes):
        raise ValueError("Saved integration credential lacks required permissions.")


def account(root, state, credentials):
    port = state["trade_port"]
    login = json.loads(read_private(root / "login.json"))
    status, response = local_request(port, "POST", "/api/auth/login", login)
    if status == 401 and not (root / "account-ready.json").exists():
        registered, _ = local_request(port, "POST", "/api/auth/register", login)
        if registered not in (201, 409):
            raise RuntimeError("Local account registration failed.")
        status, response = local_request(port, "POST", "/api/auth/login", login)
    if status != 200:
        raise RuntimeError("Saved local login was rejected; credentials were not replaced.")
    save_json(root / "account-ready.json", {"email": login["email"]})
    demo = sys.argv[3] if len(sys.argv) > 3 else None
    if demo is None:
        return
    token = response["access_token"]
    marker = root / ("imported-" + demo + ".json")
    if marker.exists():
        read_private(marker)
        return
    source = Path(__file__).resolve().parents[3] / "examples" / "regimex" / (demo + ".json")
    payload = json.loads(source.read_text())
    status, catalog = local_request(port, "GET", "/api/instruments", token=token)
    if status != 200:
        raise RuntimeError("The local instrument catalogue is unavailable.")
    for item in payload["instrument_configs"]:
        symbol = item.pop("symbol")
        matches = [
            market
            for market in catalog
            if f"{market['base_asset']['symbol']}/{market['quote_asset']['symbol']}" == symbol
            and market["is_active"]
        ]
        if len(matches) != 1:
            raise ValueError("A demo symbol does not identify one active instrument.")
        item["instrument_id"] = matches[0]["id"]
    status, existing = local_request(port, "GET", "/api/simulations", token=token)
    if status != 200:
        raise RuntimeError("Cannot inspect the local account's saved experiments.")
    matches = [item for item in existing if item["name"] == payload["name"]]
    if matches:
        from backend.app.schemas.simulation import SimulationWrite

        expected = SimulationWrite.model_validate(payload).model_dump()
        if len(matches) != 1 or SimulationWrite.model_validate(matches[0]).model_dump() != expected:
            raise ValueError(
                "An existing experiment uses this example name with different settings."
            )
        identity = matches[0]["id"]
    else:
        status, response = local_request(port, "POST", "/api/simulations", payload, token=token)
        if status != 201:
            raise RuntimeError("Demo configuration import failed.")
        identity = response["id"]
    save_json(marker, {"simulation_id": identity, "example": demo})


def health(_root, _state, _credentials):
    from regimex.service_health import worker_healthy
    from regimex.service_settings import ServiceSettings

    settings = ServiceSettings.from_env()
    queue = sys.argv[3]
    if queue not in {"analysis", "maintenance"}:
        raise ValueError("Unknown local worker queue.")
    deadline = time.monotonic() + 60
    while not worker_healthy(settings, queue):
        if time.monotonic() >= deadline:
            raise RuntimeError("A local worker did not register its heartbeat before its deadline.")
        time.sleep(0.5)


def main():
    root = Path(sys.argv[2])
    state = json.loads(read_private(root / "stack.json"))
    credentials = json.loads(read_private(root / "credentials.json"))
    {"database": database, "regimex": regimex, "account": account, "health": health}[sys.argv[1]](
        root, state, credentials
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Database exceptions may contain credentials/SQL literals. Keep private
        # command logs free of those values as well as ordinary terminal output.
        print(f"Local provisioning failed ({type(error).__name__}).", file=sys.stderr)
        raise SystemExit(1) from None
