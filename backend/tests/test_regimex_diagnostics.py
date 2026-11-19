"""Operator checks exercise a real peer without jobs, subjects or leaked credentials."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import suppress
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from backend.app.integrations.regimex.client import DiagnosticClient, TransportFailure
from backend.app.integrations.regimex.config import RegimexSettings
from backend.app.integrations.regimex.diagnostics import diagnose

TOKEN = "private-diagnostic-credential"
ROOT = Path(__file__).resolve().parents[2]


class DiagnosticPeer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.command, self.path, dict(self.headers)))
        status, body, headers = self.server.responses[self.path]
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(data)

    def log_message(self, *_args):
        pass


class RegimexDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DiagnosticPeer)
        self.server.requests = []
        self.server.responses = {
            "/health/live": (200, {"status": "alive"}, {}),
            "/health/ready": (200, {"status": "ready", "database": True}, {}),
            "/health/models": (
                200,
                {"deployment_configured": True, "worker_capability": "not_probed"},
                {},
            ),
            "/api/v1/connection": (
                200,
                {
                    "source_namespace": "trade-test",
                    "scopes": ["read", "upload", "analyze"],
                    "analysis_protocol": "regimex-analysis/1",
                },
                {},
            ),
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        with patch.dict(os.environ, {}, clear=True):
            self.settings = RegimexSettings(
                _env_file=None,
                regimex_url=f"http://127.0.0.1:{self.server.server_port}",
                regimex_token=TOKEN,
                regimex_source_id="trade-test",
                regimex_allow_loopback_http=True,
            )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_checks_use_only_read_only_paths_and_send_credentials_only_to_self_endpoint(self):
        report = diagnose(self.settings)
        self.assertEqual(report.status, "passed")
        self.assertEqual(
            {item.name: item.status for item in report.checks},
            {
                "configuration": "passed",
                "api": "passed",
                "connection": "passed",
                "source": "passed",
                "capabilities": "passed",
                "protocol": "passed",
                "database": "passed",
                "model": "passed",
                "worker": "not_checked",
            },
        )
        self.assertEqual(len(self.server.requests), 4)
        for method, path, headers in self.server.requests:
            self.assertEqual(method, "GET")
            self.assertNotIn("X-Regimex-Subject", headers)
            self.assertNotIn("X-Regimex-Run-ID", headers)
            if path == "/api/v1/connection":
                self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
            else:
                self.assertNotIn("Authorization", headers)
        self.assertNotIn(TOKEN, json.dumps(asdict(report)))

    def test_old_or_unrecognized_analysis_protocol_fails_operator_check(self):
        _, current, _ = self.server.responses["/api/v1/connection"]
        for protocol in (None, "regimex-analysis/2"):
            with self.subTest(protocol=protocol):
                account = current.copy()
                if protocol is None:
                    del account["analysis_protocol"]
                else:
                    account["analysis_protocol"] = protocol
                self.server.responses["/api/v1/connection"] = (200, account, {})
                report = diagnose(self.settings)
                self.assertEqual(report.status, "failed")
                self.assertEqual(
                    [item.code for item in report.checks if item.status == "failed"],
                    ["incompatible_protocol"],
                )

    def test_wrong_source_and_missing_permissions_are_reported_without_submitting_work(self):
        self.server.responses["/api/v1/connection"] = (
            200,
            {
                "source_namespace": "another-source",
                "analysis_protocol": "regimex-analysis/1",
                "scopes": ["upload"],
            },
            {},
        )
        report = diagnose(self.settings)
        self.assertEqual(report.status, "failed")
        self.assertEqual(
            [item.code for item in report.checks if item.status == "failed"],
            ["source_mismatch", "missing_capabilities"],
        )
        self.assertIn(
            "analyze, read",
            next(item.message for item in report.checks if item.name == "capabilities"),
        )
        self.assertTrue(all(method == "GET" for method, _, _ in self.server.requests))

    def test_rejection_and_rate_limit_do_not_echo_upstream_diagnostics_or_guess_key_lifecycle(self):
        for status, code in (
            (401, "credential_rejected"),
            (403, "access_denied"),
            (404, "endpoint_unavailable"),
            (429, "rate_limited"),
        ):
            self.server.responses["/api/v1/connection"] = (
                status,
                {"error": TOKEN + " private diagnostic"},
                {"Retry-After": "7"},
            )
            with (
                self.subTest(status=status),
                self.assertLogs("backend.app.integrations.regimex.client", level="INFO") as records,
            ):
                report = diagnose(self.settings)
            failed = next(item for item in report.checks if item.name == "connection")
            self.assertEqual(failed.code, code)
            self.assertNotIn(TOKEN, json.dumps(asdict(report)))
            self.assertNotIn(TOKEN, "".join(records.output))
            self.assertNotIn("private diagnostic", "".join(records.output))
            if status == 401:
                self.assertIn("indistinguishable", failed.message)

    def test_failed_api_probe_never_sends_the_key(self):
        self.server.responses["/health/live"] = (503, {"error": "unavailable"}, {})
        self.assertEqual(diagnose(self.settings).status, "failed")
        self.assertEqual([path for _, path, _ in self.server.requests], ["/health/live"])
        self.assertNotIn("Authorization", self.server.requests[0][2])

    def test_invalid_metadata_and_numeric_booleans_cannot_claim_readiness(self):
        for path, body in (
            ("/api/v1/connection", {"source_namespace": "trade-test", "scopes": ["read", "read"]}),
            (
                "/api/v1/connection",
                {"source_namespace": "trade-test", "scopes": [{"invalid": True}]},
            ),
            ("/health/ready", {"status": "ready", "database": 1}),
            ("/health/models", {"deployment_configured": 1, "worker_capability": "not_probed"}),
        ):
            previous = self.server.responses[path]
            self.server.responses[path] = (200, body, {})
            with self.subTest(path=path):
                self.assertEqual(diagnose(self.settings).status, "failed")
            self.server.responses[path] = previous

    def test_diagnostics_enforce_a_small_response_limit_and_one_shared_deadline(self):
        captured = []

        def client(connection):
            captured.append(connection)
            return DiagnosticClient(connection)

        self.server.responses["/api/v1/connection"] = (200, {"padding": "x" * (64 * 1024)}, {})
        with patch("backend.app.integrations.regimex.diagnostics.DiagnosticClient", client):
            report = diagnose(self.settings)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].response_bytes, 64 * 1024)
        self.assertEqual(captured[0].total_timeout, 10)
        self.assertEqual(report.status, "failed")

    def test_safe_transport_failure_categories_are_actionable(self):
        for category in (
            "certificate_rejected",
            "tls_failed",
            "dns_failed",
            "timeout",
            "connection_failed",
            "invalid_protocol",
        ):
            with (
                self.subTest(category=category),
                patch.object(DiagnosticClient, "json", side_effect=TransportFailure(category)),
            ):
                report = diagnose(self.settings)
            self.assertEqual(report.status, "failed")
            self.assertEqual(report.checks[1].code, category)

    def test_diagnostic_transport_cannot_mutate_or_select_arbitrary_endpoints(self):
        client = DiagnosticClient(self.settings.regimex_connection())
        for method, path in (
            ("POST", "/api/v1/connection"),
            ("GET", "/api/v1/integration/analyses"),
            ("GET", "https://example.test"),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(ValueError):
                client.json(method, path)
        self.assertEqual(self.server.requests, [])


class RegimexDoctorCommandTests(unittest.TestCase):
    def command(self, environment):
        with tempfile.TemporaryDirectory() as directory:
            return subprocess.run(
                [sys.executable, "-m", "backend.scripts.regimex_doctor", "--json"],
                cwd=directory,
                env={"PYTHONPATH": str(ROOT), **environment},
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

    def test_disabled_connection_does_not_require_simulator_database_or_auth_secrets(self):
        result = self.command({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "disabled")

    def test_invalid_configuration_is_reported_without_printing_values_or_traceback(self):
        result = self.command(
            {
                "REGIMEX_URL": "https://secret-user:private-password@private-host.test",
                "REGIMEX_TOKEN": TOKEN,
            }
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["checks"][0]["code"], "invalid_configuration")
        for private in (TOKEN, "private-password", "secret-user", "private-host.test", "Traceback"):
            self.assertNotIn(private, result.stdout + result.stderr)
