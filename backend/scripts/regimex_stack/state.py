"""Persistent local identities, private credentials and explicit child environments."""

import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

from .files import lock_is_held, private_directory, read_private, save_json, write_private
from .options import REPOSITORY


def load_or_create(args):
    root = args.root
    for name in ("logs", "objects", "work"):
        private_directory(root / name)
    requested = {
        "format": 1,
        "regimex_checkout": str(args.regimex_checkout),
        "model_bundle": str(args.model_bundle),
        "manifest_sha256": args.manifest_sha256,
        "trade_port": args.trade_port,
        "regimex_port": args.regimex_port,
        "postgres_port": args.postgres_port,
        "postgres_bin": str(args.postgres_bin),
    }
    state_path = root / "stack.json"
    if state_path.exists():
        state = json.loads(read_private(state_path))
        if any(state.get(key) != value for key, value in requested.items()):
            raise ValueError(
                "This stack belongs to different inputs or ports. "
                "Use its original options or a new --root."
            )
        credentials = json.loads(read_private(root / "credentials.json"))
        read_private(root / "login.json")
    else:
        # Refuse partial state rather than silently generating replacement keys.
        if (root / "credentials.json").exists() or (root / "postgres").exists():
            raise ValueError("Incomplete bootstrap state exists; inspect it before retrying.")
        state = {**requested, "source_id": "trade-local-" + uuid4().hex}
        credentials = {
            key: secrets.token_urlsafe(48)
            for key in (
                "database_admin",
                "database_trade",
                "database_regimex",
                "jwt_secret",
                "pepper",
            )
        }
        save_json(root / "credentials.json", credentials)
        save_json(
            root / "login.json",
            {"email": "local@example.com", "password": secrets.token_urlsafe(24)},
        )
        save_json(state_path, state)
    write_private(root / "database-admin.password", credentials["database_admin"] + "\n")
    write_private(root / "regimex.dsn", database_url(state, credentials, "regimex") + "\n")
    return state, credentials


def database_url(state, credentials, name, *, sqlalchemy=False):
    scheme = "postgresql+psycopg" if sqlalchemy else "postgresql"
    # Generated URL-safe passwords contain no URL delimiters.
    password = credentials["database_" + name]
    return f"{scheme}://stack_{name}:{password}@127.0.0.1:{state['postgres_port']}/{name}"


def base_environment():
    blocked = {
        "DATABASE_URL",
        "PASSWORD_PEPPER",
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "UV_PROJECT_ENVIRONMENT",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in blocked and not key.startswith(("REGIMEX_", "JWT_", "PG", "TRADE_STACK_"))
    }
    environment["PYTHONPATH"] = str(REPOSITORY)
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def service_environments(root, state, credentials):
    common = base_environment()
    # Dependency installation may need the operator's package proxy. Runtime
    # services never inherit proxy or custom trust settings from that toolchain.
    common = {
        key: value
        for key, value in common.items()
        if key.upper()
        not in {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
    }
    regimex = {
        **common,
        "REGIMEX_DATABASE_DSN_FILE": str(root / "regimex.dsn"),
        "REGIMEX_DATABASE_SCHEMA": "public",
        "REGIMEX_OBJECT_ROOT": str(root / "objects"),
        "REGIMEX_WORK_ROOT": str(root / "work"),
        "REGIMEX_PUBLIC_ORIGIN": f"http://127.0.0.1:{state['regimex_port']}",
        "REGIMEX_ALLOW_LOOPBACK_HTTP": "true",
    }
    trade = {
        **common,
        "DATABASE_URL": database_url(state, credentials, "trade", sqlalchemy=True),
        "JWT_SECRET": credentials["jwt_secret"],
        "JWT_ALGORITHM": "HS256",
        "JWT_EXPIRE_MINUTES": "30",
        "PASSWORD_PEPPER": credentials["pepper"],
        "REGIMEX_URL": regimex["REGIMEX_PUBLIC_ORIGIN"],
        "REGIMEX_SOURCE_ID": state["source_id"],
        "REGIMEX_ALLOW_LOOPBACK_HTTP": "true",
        "REGIMEX_MAX_CONCURRENT_REQUESTS": "4",
        "TRADE_STACK_FRONTEND": str(root / "frontend" / "dist"),
    }
    return trade, regimex


def process_identity(pid):
    # /proc fields after the closing command-name parenthesis start at field 3;
    # starttime is field 22. A reused PID cannot stand in for the supervisor.
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def status(root):
    path = root / "status.json"
    if not path.exists():
        print("Stack has no saved supervisor state.")
        return 1
    saved = json.loads(read_private(path))
    if saved.get("state") == "stopped":
        print("Stack is stopped.")
        return 1
    pid = saved.get("pid")
    identity = saved.get("process_identity")
    if (
        type(pid) is not int
        or not isinstance(identity, str)
        or process_identity(pid) != identity
        or not lock_is_held(root)
    ):
        print("Stack state is stale; inspect its private logs and occupied ports.")
        return 2
    print(f"Stack supervisor is running; UI: http://127.0.0.1:{saved['trade_port']}")
    return 0
