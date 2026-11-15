"""Validate local prerequisites before installing or starting anything."""

import argparse
import hashlib
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
DEMOS = ("growth", "decline", "mixed", "reversal")


def arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Run a private local Trade + Regimex stack (Linux/WSL)."
    )
    parser.add_argument("--regimex-checkout", type=Path)
    parser.add_argument("--model-bundle", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument(
        "--postgres-bin", type=Path, help="PostgreSQL 16 bin directory; otherwise search PATH"
    )
    parser.add_argument("--root", type=Path, default=REPOSITORY / "var" / "stack")
    parser.add_argument("--trade-port", type=int, default=18001)
    parser.add_argument("--regimex-port", type=int, default=18002)
    parser.add_argument("--postgres-port", type=int, default=55445)
    parser.add_argument(
        "--demo",
        choices=DEMOS,
        help="Import one static example configuration into the local account",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate inputs without installing or starting services",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="Report saved supervisor state; exit 0 running, 1 stopped, 2 stale/invalid",
    )
    mode.add_argument(
        "--doctor",
        action="store_true",
        help="Diagnose this stack's backend connection without creating analyses",
    )
    args = parser.parse_args(argv)
    if args.root.is_symlink():
        parser.error("--root cannot be a symbolic link")
    args.root = args.root.expanduser().resolve()
    if not (args.status or args.doctor) and any(
        value is None for value in (args.regimex_checkout, args.model_bundle, args.manifest_sha256)
    ):
        parser.error("--regimex-checkout, --model-bundle and --manifest-sha256 are required")
    return args


def executable(name):
    found = shutil.which(name)
    if not found:
        raise ValueError(f"Required executable is missing from PATH: {name}")
    return found


def version(command):
    return subprocess.check_output(
        command, text=True, timeout=10, stderr=subprocess.DEVNULL
    ).strip()


def check_ports(ports):
    if len(set(ports)) != len(ports) or any(not 1024 <= port <= 65535 for port in ports):
        raise ValueError("Ports must be distinct unprivileged TCP ports (1024-65535).")
    for port in ports:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise ValueError(
                    f"Local port {port} is already occupied; no existing service will be adopted."
                ) from None


def preflight(args):
    if os.name != "posix" or not Path("/proc/self/stat").is_file():
        raise ValueError("The local runner supports Linux and WSL only.")
    if os.getuid() == 0:
        raise ValueError("Run as a regular user; PostgreSQL refuses superuser execution.")
    args.regimex_checkout = args.regimex_checkout.expanduser().resolve()
    args.model_bundle = args.model_bundle.expanduser().resolve()
    for filename in (
        "uv.lock",
        "pyproject.toml",
        ".python-version",
        "src/regimex/service_bootstrap.py",
    ):
        if not (args.regimex_checkout / filename).is_file():
            raise ValueError("--regimex-checkout must contain a complete trusted Regimex checkout.")
    if args.root.is_relative_to(REPOSITORY) and not args.root.is_relative_to(REPOSITORY / "var"):
        raise ValueError(
            "Runtime state inside the Trade checkout must live under its ignored var directory."
        )
    if args.root.is_relative_to(args.regimex_checkout) or args.regimex_checkout.is_relative_to(
        args.root
    ):
        raise ValueError("Runtime state and the Regimex checkout must not overlap.")
    if args.model_bundle.is_relative_to(args.root) or args.root.is_relative_to(args.model_bundle):
        raise ValueError("The supplied model bundle and runtime state must not overlap.")
    manifest = args.model_bundle / "manifest.json"
    if not re.fullmatch(r"[a-fA-F0-9]{64}", args.manifest_sha256):
        raise ValueError("--manifest-sha256 must be an explicitly trusted SHA-256 digest.")
    args.manifest_sha256 = args.manifest_sha256.lower()
    if not manifest.is_file() or manifest.stat().st_size > 1024 * 1024:
        raise ValueError("The model bundle has no bounded manifest.json.")
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != args.manifest_sha256:
        raise ValueError("The supplied model manifest does not match its expected digest.")
    args.python_version = (args.regimex_checkout / ".python-version").read_text().strip()
    if not re.fullmatch(r"3\.14\.\d+", args.python_version):
        raise ValueError("This runner expects Regimex's pinned Python 3.14 patch version.")
    args.uv, args.npm = executable("uv"), executable("npm")
    node = version([executable("node"), "--version"])
    if not re.fullmatch(r"v24\.\d+\.\d+", node):
        raise ValueError("Node.js 24 is required for the locked frontend toolchain.")
    args.postgres_bin = (
        (args.postgres_bin or Path(executable("postgres")).parent).expanduser().resolve()
    )
    for binary in ("postgres", "initdb"):
        path = args.postgres_bin / binary
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError("--postgres-bin must contain executable postgres and initdb binaries.")
    if not re.fullmatch(
        r"postgres \(PostgreSQL\) 16\.\d+(?:\s.*)?",
        version([args.postgres_bin / "postgres", "--version"]),
    ):
        raise ValueError("PostgreSQL 16 is required for this local stack.")
    check_ports((args.trade_port, args.regimex_port, args.postgres_port))
