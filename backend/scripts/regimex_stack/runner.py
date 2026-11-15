"""Install locked dependencies and supervise the local two-service stack."""

import os
import shutil
import signal
import time

from .files import private_directory, read_private, save_json, stack_lock
from .options import REPOSITORY, preflight
from .processes import Supervisor
from .state import base_environment, load_or_create, process_identity, service_environments


def dependencies(supervisor, args):
    root, environment = args.root, base_environment()
    trade_python = root / "trade-venv" / "bin" / "python"
    regimex_python = root / "regimex-venv" / "bin" / "python"
    print("Installing locked Python and frontend dependencies...", flush=True)
    if not trade_python.exists():
        supervisor.run(
            "trade-environment",
            [args.uv, "venv", "--python", args.python_version, root / "trade-venv"],
            environment,
            root,
            timeout=900,
        )
    supervisor.run(
        "trade-dependencies",
        [
            args.uv,
            "pip",
            "sync",
            "--python",
            trade_python,
            REPOSITORY / "backend" / "requirements.lock",
        ],
        environment,
        root,
        timeout=900,
    )
    supervisor.run(
        "regimex-dependencies",
        [
            args.uv,
            "sync",
            "--project",
            args.regimex_checkout,
            "--locked",
            "--no-dev",
            "--extra",
            "service",
            "--extra",
            "ml",
            "--extra",
            "viz",
        ],
        {**environment, "UV_PROJECT_ENVIRONMENT": str(root / "regimex-venv")},
        root,
        timeout=1800,
    )
    frontend = copy_frontend(root)
    supervisor.run(
        "frontend-dependencies",
        [args.npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        environment,
        frontend,
        timeout=900,
    )
    supervisor.run(
        "frontend-build",
        [args.npm, "run", "build"],
        environment,
        frontend,
        timeout=300,
    )
    return trade_python, regimex_python


def copy_frontend(root):
    """Copy only build inputs, without .env, generated assets or developer state."""
    target = root / "frontend"
    private_directory(target)
    source = REPOSITORY / "frontend"
    for name in (
        "package.json",
        "package-lock.json",
        "index.html",
        "vite.config.ts",
        "tsconfig.json",
        "tsconfig.app.json",
        "tsconfig.node.json",
    ):
        shutil.copyfile(source / name, target / name)
    for name in ("src", "public"):
        destination = target / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source / name, destination)
    return target


def postgres(supervisor, args, trade_python):
    root, environment = args.root, base_environment()
    directory = root / "postgres"
    if not (directory / "PG_VERSION").exists():
        if directory.exists() and any(directory.iterdir()):
            raise ValueError(
                "PostgreSQL initialization is incomplete; its files were preserved for inspection."
            )
        supervisor.run(
            "postgres-initialize",
            [
                args.postgres_bin / "initdb",
                "-D",
                directory,
                "--username=stack_admin",
                f"--pwfile={root / 'database-admin.password'}",
                "--auth-host=scram-sha-256",
                "--auth-local=scram-sha-256",
                "--no-locale",
                "--encoding=UTF8",
            ],
            environment,
            root,
        )
    elif (directory / "PG_VERSION").read_text().strip() != "16":
        raise ValueError("The saved PostgreSQL cluster has an incompatible major version.")
    supervisor.start(
        "postgres",
        [
            args.postgres_bin / "postgres",
            "-D",
            directory,
            "-h",
            "127.0.0.1",
            "-p",
            str(args.postgres_port),
            "-k",
            "",
        ],
        environment,
        root,
    )
    supervisor.run(
        "database-bootstrap",
        [trade_python, "-m", "backend.scripts.regimex_stack.provision", "database", root],
        environment,
        root,
        timeout=45,
    )


def services(supervisor, args, state, credentials, trade_python, regimex_python):
    root = args.root
    trade, regimex = service_environments(root, state, credentials)
    supervisor.run(
        "model-bootstrap",
        [regimex_python, "-m", "backend.scripts.regimex_stack.provision", "regimex", root],
        regimex,
        root,
        timeout=180,
    )
    trade["REGIMEX_TOKEN"] = read_private(root / "integration.token").strip()
    supervisor.run(
        "trade-migrate",
        [trade_python, "-m", "alembic", "-c", REPOSITORY / "alembic.ini", "upgrade", "head"],
        trade,
        root,
    )
    if not (root / "market-ready.json").exists():
        supervisor.run(
            "market-bootstrap", [trade_python, "-m", "backend.scripts.seed_market"], trade, root
        )
        save_json(root / "market-ready.json", {"initialized": True})
    supervisor.start(
        "regimex-api",
        [
            regimex_python,
            "-m",
            "regimex",
            "service",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.regimex_port),
        ],
        regimex,
        root,
    )
    for queue in ("analysis", "maintenance"):
        supervisor.start(
            "regimex-" + queue,
            [regimex_python, "-m", "regimex", "service", "worker", "--queue", queue],
            regimex,
            root,
        )
    supervisor.start(
        "trade-api",
        [
            trade_python,
            "-m",
            "uvicorn",
            "backend.scripts.regimex_stack.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.trade_port),
            "--no-access-log",
        ],
        trade,
        root,
    )
    supervisor.start(
        "trade-worker",
        [trade_python, "-m", "backend.scripts.run_simulation_workers", "--workers", "1"],
        trade,
        root,
    )
    supervisor.ready(args.regimex_port, "/health/ready")
    supervisor.ready(args.trade_port, "/health")
    # Readiness verifies database/store access; model and worker capabilities have
    # their own native checks, including each exact queue heartbeat.
    supervisor.ready(args.regimex_port, "/health/models")
    for queue in ("analysis", "maintenance"):
        supervisor.run(
            "health-" + queue,
            [
                regimex_python,
                "-m",
                "backend.scripts.regimex_stack.provision",
                "health",
                root,
                queue,
            ],
            regimex,
            root,
        )
    command = [trade_python, "-m", "backend.scripts.regimex_stack.provision", "account", root]
    if args.demo:
        command.append(args.demo)
    supervisor.run("account-bootstrap", command, trade, root)
    supervisor.check()


def interrupted(_signum, _frame):
    raise KeyboardInterrupt


def run(args):
    preflight(args)
    if args.check:
        print("Local prerequisites and model manifest digest are valid. No services were started.")
        return 0
    private_directory(args.root)
    with stack_lock(args.root):
        state, credentials = load_or_create(args)
        supervisor = Supervisor(args.root)
        previous_handler = signal.signal(signal.SIGTERM, interrupted)
        try:
            trade_python, regimex_python = dependencies(supervisor, args)
            print("Starting private PostgreSQL, APIs and workers...", flush=True)
            postgres(supervisor, args, trade_python)
            services(supervisor, args, state, credentials, trade_python, regimex_python)
            save_json(
                args.root / "status.json",
                {
                    "state": "running",
                    "pid": os.getpid(),
                    "process_identity": process_identity(os.getpid()),
                    "trade_port": args.trade_port,
                },
            )
            print(f"Trade UI: http://127.0.0.1:{args.trade_port}", flush=True)
            print(f"UI login credentials: {args.root / 'login.json'}", flush=True)
            print(f"Private service logs: {args.root / 'logs'}", flush=True)
            print(
                "Press Ctrl+C to stop all owned services. State is preserved for restart.",
                flush=True,
            )
            while True:
                supervisor.check()
                time.sleep(1)
        except KeyboardInterrupt:
            print("Stopping the local stack...", flush=True)
        finally:
            # Ignore a repeated interrupt while terminating the owned groups.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            previous_interrupt = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                supervisor.close()
            finally:
                signal.signal(signal.SIGINT, previous_interrupt)
                signal.signal(signal.SIGTERM, previous_handler)
    return 0
