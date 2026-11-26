"""Local-runner ownership, persistent identities and bounded setup behavior."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.scripts.regimex_stack.files import (
    private_directory,
    read_private,
    save_json,
    stack_lock,
    write_private,
)
from backend.scripts.regimex_stack.options import (
    DEMOS,
    REPOSITORY,
    arguments,
    check_ports,
    preflight,
)
from backend.scripts.regimex_stack.processes import Supervisor
from backend.scripts.regimex_stack.runner import copy_frontend, services
from backend.scripts.regimex_stack.state import (
    load_or_create,
    process_identity,
    service_environments,
    status,
)


class LocalStackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def args(self):
        checkout = self.root / "regimex"
        for name in ("uv.lock", "pyproject.toml", "src/regimex/service_bootstrap.py"):
            path = checkout / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        (checkout / ".python-version").write_text("3.14.4\n")
        bundle = self.root / "bundle"
        bundle.mkdir(exist_ok=True)
        (bundle / "manifest.json").write_text("{}")
        args = arguments(
            [
                "--root",
                str(self.root / "state"),
                "--regimex-checkout",
                str(checkout),
                "--model-bundle",
                str(bundle),
                "--manifest-sha256",
                hashlib.sha256(b"{}").hexdigest(),
            ]
        )
        args.postgres_bin = Path("/private/postgres/bin")
        return args

    def test_private_state_rejects_public_files_symlinks_and_competing_runner(self):
        path = self.root / "credential"
        write_private(path, "private-value")
        self.assertEqual(read_private(path), "private-value")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            read_private(path)
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaises(OSError):
            read_private(link)
        with self.assertRaisesRegex(ValueError, "symbolic"):
            write_private(link, "replacement")
        with (
            stack_lock(self.root),
            self.assertRaisesRegex(ValueError, "Another runner"),
            stack_lock(self.root),
        ):
            pass

    def test_restart_preserves_identities_and_separates_ui_credentials(self):
        args = self.args()
        private_directory(args.root)
        first, credentials = load_or_create(args)
        login = read_private(args.root / "login.json")
        second, repeated = load_or_create(args)
        self.assertEqual((first, credentials), (second, repeated))
        self.assertEqual(read_private(args.root / "login.json"), login)
        self.assertEqual(set(json.loads(login)), {"email", "password"})
        for value in credentials.values():
            self.assertNotIn(value, login)
        args.trade_port += 1
        with self.assertRaisesRegex(ValueError, "different inputs"):
            load_or_create(args)
        self.assertEqual(json.loads(read_private(args.root / "credentials.json")), credentials)

    def test_bootstrap_resumes_after_each_published_file(self):
        args = self.args()
        for filename in ("stack.json", "credentials.json", "login.json"):
            with self.subTest(filename=filename):
                args.root = self.root / filename.removesuffix(".json")
                private_directory(args.root)

                def interrupt(path, value, stop_after=filename):
                    save_json(path, value)
                    if path.name == stop_after:
                        raise InterruptedError

                with (
                    patch("backend.scripts.regimex_stack.state.save_json", side_effect=interrupt),
                    self.assertRaises(InterruptedError),
                ):
                    load_or_create(args)
                saved = {file: read_private(file) for file in args.root.glob("*.json")}
                load_or_create(args)
                for path, original in saved.items():
                    self.assertEqual(read_private(path), original)
                self.assertTrue((args.root / "login.json").is_file())
                self.assertTrue((args.root / "regimex.dsn").is_file())

    def test_recovers_partial_credentials_only_before_postgres_exists(self):
        args = self.args()
        private_directory(args.root)
        _state, credentials = load_or_create(args)
        login = read_private(args.root / "login.json")
        (args.root / "stack.json").unlink()
        self.assertEqual(load_or_create(args)[1], credentials)
        self.assertEqual(read_private(args.root / "login.json"), login)
        private_directory(args.root / "postgres")
        (args.root / "credentials.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing its stack identity or credentials"):
            load_or_create(args)
        self.assertFalse((args.root / "credentials.json").exists())

    def test_manifest_mismatch_fails_before_dependency_installation(self):
        args = self.args()
        args.manifest_sha256 = "0" * 64
        with self.assertRaisesRegex(ValueError, "expected digest"):
            preflight(args)
        self.assertFalse(args.root.exists())

    @unittest.skipUnless(shutil.which("uv"), "requires the local stack's uv executable")
    def test_check_enforces_the_selected_checkouts_uv_requirement(self):
        args = self.args()
        (args.regimex_checkout / "pyproject.toml").write_text(
            '[project]\nname="regimex"\nversion="1.0"\n[tool.uv]\nrequired-version="==0.0.1"\n'
        )
        with (
            patch("backend.scripts.regimex_stack.options.executable", side_effect=lambda name: name),
            self.assertRaisesRegex(ValueError, "uv version required by that checkout"),
        ):
            preflight(args)
        self.assertFalse((args.regimex_checkout / ".venv").exists())
        self.assertFalse(args.root.exists())

    def test_occupied_and_duplicate_ports_cannot_be_adopted(self):
        with self.assertRaisesRegex(ValueError, "distinct"):
            check_ports((19001, 19001, 19002))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            with self.assertRaisesRegex(ValueError, "occupied"):
                check_ports((listener.getsockname()[1],))

    def test_runtime_environment_does_not_inherit_other_service_or_proxy_settings(self):
        args = self.args()
        private_directory(args.root)
        state, credentials = load_or_create(args)
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "other-database",
                "JWT_ALGORITHM": "none",
                "REGIMEX_DATABASE_DSN": "other-regimex-database",
                "REGIMEX_S3_BUCKET": "other-bucket",
                "PYTHONPATH": "/other/code",
                "HTTPS_PROXY": "http://other-proxy",
                "PGPASSWORD": "other-password",
            },
        ):
            trade, regimex = service_environments(args.root, state, credentials)
        for environment in (trade, regimex):
            self.assertNotIn("REGIMEX_S3_BUCKET", environment)
            self.assertNotIn("PGPASSWORD", environment)
            self.assertNotIn("HTTPS_PROXY", environment)
            self.assertEqual(environment["PYTHONPATH"], str(REPOSITORY))
        self.assertEqual(trade["JWT_ALGORITHM"], "HS256")
        self.assertNotIn("REGIMEX_DATABASE_DSN", regimex)
        self.assertEqual(regimex["REGIMEX_DATABASE_DSN_FILE"], str(args.root / "regimex.dsn"))

    def test_status_requires_process_identity_and_live_lock(self):
        save_json(
            self.root / "status.json",
            {
                "state": "running",
                "pid": os.getpid(),
                "process_identity": process_identity(os.getpid()),
                "trade_port": 18001,
            },
        )
        with contextlib.redirect_stdout(io.StringIO()), stack_lock(self.root):
            self.assertEqual(status(self.root), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(status(self.root), 2)
        save_json(self.root / "status.json", {"state": "stopped"})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(status(self.root), 1)

    def test_supervisor_terminates_child_groups_on_failed_setup(self):
        private_directory(self.root / "logs")
        supervisor = Supervisor(self.root)
        child = supervisor.start(
            "owned",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            dict(os.environ),
            self.root,
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "failed"):
                supervisor.run(
                    "failed",
                    [sys.executable, "-c", "raise SystemExit(1)"],
                    dict(os.environ),
                    self.root,
                )
        finally:
            supervisor.close()
        self.assertIsNotNone(child.poll())
        self.assertEqual(json.loads(read_private(self.root / "status.json")), {"state": "stopped"})

    def test_failed_connection_check_stops_startup_before_trade_opens(self):
        args = self.args()
        private_directory(args.root)
        state, credentials = load_or_create(args)
        write_private(args.root / "integration.token", "local-test-key\n")
        supervisor = Mock(spec=Supervisor)
        trade_python = args.root / "trade-venv/bin/python"

        def run(name, command, environment, cwd, **kwargs):
            if command == [trade_python, "-m", "backend.scripts.regimex_doctor"]:
                self.assertEqual(environment["REGIMEX_TOKEN"], "local-test-key")
                self.assertEqual(environment["REGIMEX_SOURCE_ID"], state["source_id"])
                raise RuntimeError("Connection check failed")

        supervisor.run.side_effect = run
        with self.assertRaisesRegex(RuntimeError, "Connection check failed"):
            services(supervisor, args, state, credentials, trade_python, "regimex-python")
        self.assertNotIn("trade-api", [item.args[0] for item in supervisor.start.call_args_list])

    def test_cleanup_includes_descendants_after_the_leader_exits(self):
        private_directory(self.root / "logs")
        supervisor = Supervisor(self.root)
        marker = self.root / "descendant.pid"
        script = (
            "import subprocess,sys; from pathlib import Path; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "Path(sys.argv[1]).write_text(str(child.pid))"
        )
        leader = supervisor.start(
            "descendant", [sys.executable, "-c", script, marker], dict(os.environ), self.root
        )
        try:
            deadline = time.monotonic() + 5
            while supervisor.exit_info(leader) is None:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            child_pid = int(marker.read_text())
            self.assertIsNotNone(process_identity(child_pid))
        finally:
            supervisor.close()
        deadline = time.monotonic() + 5
        while (stat_path := Path(f"/proc/{child_pid}/stat")).exists():
            if stat_path.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

    def test_frontend_build_copy_excludes_checkout_environment_and_artifacts(self):
        source = self.root / "checkout" / "frontend"
        source.mkdir(parents=True)
        for name in (
            "package.json",
            "package-lock.json",
            "index.html",
            "vite.config.ts",
            "tsconfig.json",
            "tsconfig.app.json",
            "tsconfig.node.json",
            ".env",
        ):
            (source / name).write_text(name)
        for name in ("src", "public", "node_modules", "dist"):
            (source / name).mkdir()
            (source / name / "test").write_text(name)
        destination = self.root / "runtime"
        destination.mkdir()
        with patch("backend.scripts.regimex_stack.runner.REPOSITORY", source.parent):
            copied = copy_frontend(destination)
        self.assertEqual((copied / "src" / "test").read_text(), "src")
        for name in (".env", "node_modules", "dist"):
            self.assertFalse((copied / name).exists())
            self.assertTrue((source / name).exists())

    def test_examples_are_static_native_configs_with_symbol_selectors(self):
        # Symbol lookup replaces only instrument_id; native validation and engine
        # retain complete control of prices and every submitted order.
        from backend.app.schemas.simulation import SimulationWrite

        for name in DEMOS:
            payload = json.loads(
                (REPOSITORY / "examples" / "regimex" / (name + ".json")).read_text()
            )
            for index, instrument in enumerate(payload["instrument_configs"], start=1):
                self.assertEqual(instrument.pop("symbol"), "BTC/USD")
                self.assertNotIn("instrument_id", instrument)
                instrument["instrument_id"] = index
            parsed = SimulationWrite.model_validate(payload)
            self.assertGreaterEqual(parsed.duration_steps, 512)
            self.assertLessEqual(parsed.duration_steps, 1024)


if __name__ == "__main__":
    unittest.main()
