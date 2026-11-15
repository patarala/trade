"""A real HTTP peer exercises time, framing, size and credential boundaries."""

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from pydantic import SecretStr

from backend.app.integrations.regimex.client import RegimexClient, TransportFailure
from backend.app.integrations.regimex.config import Connection
from backend.app.integrations.regimex.errors import GatewayError


class Peer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.received.append((self.command, self.path, dict(self.headers)))
        self.send_response(self.server.status)
        for key, value in self.server.headers:
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for block in self.server.blocks:
                if self.server.delay:
                    time.sleep(self.server.delay)
                self.wfile.write(block)
                self.wfile.flush()
        except BrokenPipeError, ConnectionResetError:
            pass
        self.close_connection = True

    def do_POST(self):
        self.server.uploads.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.do_GET()

    def log_message(self, *_args):
        pass


class RegimexHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
        self.server.received, self.server.uploads = [], []
        self.server.status = 200
        self.server.headers = [("Content-Type", "application/json")]
        self.server.blocks, self.server.delay = [b'{"ok":true}'], 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = Connection(
            f"http://127.0.0.1:{self.server.server_port}",
            SecretStr("private-server-token"),
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def client(self, **options):
        return RegimexClient(replace(self.connection, **options), subject="7", run_id="42")

    def test_only_backend_credentials_and_verified_claims_are_sent(self):
        value = self.client().json(
            "POST",
            "/api/v1/integration/analyses",
            payload={"snapshot_id": "fixture"},
            idempotency_key="trade-" + "a" * 64,
        )
        self.assertEqual(value, {"ok": True})
        method, path, headers = self.server.received[0]
        self.assertEqual((method, path), ("POST", "/api/v1/integration/analyses"))
        self.assertEqual(headers["Authorization"], "Bearer private-server-token")
        self.assertEqual(headers["X-Regimex-Subject"], "7")
        self.assertEqual(headers["X-Regimex-Run-ID"], "42")
        self.assertNotIn("Origin", headers)
        self.assertNotIn("Cookie", headers)
        self.assertEqual(self.server.uploads, [b'{"snapshot_id":"fixture"}'])

    def test_redirects_never_forward_credentials(self):
        self.server.status = 302
        self.server.headers += [("Location", "https://other.example.test/stolen")]
        with self.assertRaises(GatewayError) as raised:
            self.client().json("GET", "/api/v1/integration/analyses?limit=1")
        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(len(self.server.received), 1)
        self.assertNotIn("stolen", str(raised.exception))

    def test_rate_limit_preserves_only_bounded_retry_delay(self):
        self.server.status = 429
        self.server.headers += [("Retry-After", "12")]
        self.server.blocks = [b"raw upstream secret"]
        with self.assertRaises(GatewayError) as raised:
            self.client().json("GET", "/api/v1/integration/analyses?limit=1")
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(raised.exception.retry_after, 12)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("secret", str(raised.exception))

    def test_error_bodies_are_not_copied_to_the_simulator(self):
        for status in (401, 403, 404, 409, 422, 500):
            self.server.status = status
            self.server.blocks = [b"private-backend-token secret diagnostics"]
            with self.subTest(status=status), self.assertRaises(GatewayError) as raised:
                self.client().json("GET", "/api/v1/integration/analyses?limit=1")
            self.assertNotIn("private", str(raised.exception))
            self.assertNotIn("secret", str(raised.exception))

    def test_size_encoding_and_ambiguous_framing_are_rejected(self):
        for headers in (
            [("Content-Length", "1025")],
            [("Content-Encoding", "gzip")],
            [("Content-Length", "0"), ("Content-Length", "0")],
            [("Content-Length", "0"), ("Transfer-Encoding", "chunked")],
        ):
            self.server.headers = [("Content-Type", "application/json"), *headers]
            with self.subTest(headers=headers), self.assertRaises(GatewayError):
                self.client(response_bytes=1024).json("GET", "/api/v1/integration/analyses?limit=1")
        self.server.headers = [("Content-Type", "application/json")]
        self.server.blocks = [b"x" * 1025]
        with self.assertRaises(GatewayError):
            self.client(response_bytes=1024).json("GET", "/api/v1/integration/analyses?limit=1")

    def test_invalid_json_and_content_types_are_rejected(self):
        for body in (b'{"key":1,"key":2}', b'{"value":NaN}', b'{"value":1e999}', b"[]", b"\xff"):
            self.server.blocks = [body]
            with self.subTest(body=body), self.assertRaises(GatewayError) as raised:
                self.client().json("GET", "/api/v1/integration/analyses?limit=1")
            self.assertEqual(raised.exception.status, 502)
        self.server.blocks = [b'{"ok":true}']
        self.server.headers = [("Content-Type", "text/html")]
        with self.assertRaises(GatewayError):
            self.client().json("GET", "/api/v1/integration/analyses?limit=1")

    def test_total_deadline_stops_a_peer_that_keeps_sending_small_chunks(self):
        self.server.blocks = [b" "] * 20
        self.server.delay = 0.05
        client = self.client(read_timeout=1, total_timeout=0.18)
        started = time.monotonic()
        with self.assertRaises(GatewayError):
            client.json("GET", "/api/v1/integration/analyses?limit=1")
        self.assertLess(time.monotonic() - started, 0.8)

    def test_deadline_is_shared_across_requests(self):
        client = self.client(total_timeout=0.05)
        time.sleep(0.06)
        with self.assertRaises(GatewayError):
            client.json("GET", "/api/v1/integration/analyses?limit=1")
        self.assertEqual(self.server.received, [])

    def test_tls_handshake_respects_the_remaining_total_deadline(self):
        received, release = threading.Event(), threading.Event()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(2)

            def stalled_tls_peer():
                with listener.accept()[0] as peer:
                    peer.settimeout(2)
                    if peer.recv(65536):
                        received.set()
                    release.wait(2)

            thread = threading.Thread(target=stalled_tls_peer, daemon=True)
            thread.start()
            context = ssl.create_default_context()

            def delayed_context():
                # Certificate-store setup consumes part of the same deadline.
                time.sleep(0.6)
                return context

            client = self.client(
                url=f"https://127.0.0.1:{listener.getsockname()[1]}",
                connect_timeout=1,
                total_timeout=1.0,
            )
            started = time.monotonic()
            try:
                with (
                    patch(
                        "backend.app.integrations.regimex.client.ssl.create_default_context",
                        delayed_context,
                    ),
                    self.assertRaises(GatewayError),
                ):
                    client.json("GET", "/api/v1/integration/analyses?limit=1")
                self.assertTrue(received.is_set())
                self.assertLess(time.monotonic() - started, 1.4)
            finally:
                release.set()
                thread.join(timeout=2)

    def test_https_verifies_certificate_and_hostname_before_sending_credentials(self):
        openssl = shutil.which("openssl")
        if openssl is None:
            self.skipTest("openssl is required to generate the temporary TLS certificate")
        with tempfile.TemporaryDirectory(prefix="trade-regimex-tls-") as directory:
            certificate, key = Path(directory) / "certificate.pem", Path(directory) / "key.pem"
            subprocess.run(
                [
                    openssl,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=127.0.0.1",
                    "-addext",
                    "subjectAltName=IP:127.0.0.1",
                    "-keyout",
                    str(key),
                    "-out",
                    str(certificate),
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            server.received, server.uploads = [], []
            server.status = 200
            server.headers = [("Content-Type", "application/json")]
            server.blocks, server.delay = [b'{"ok":true}'], 0
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"https://127.0.0.1:{server.server_port}"
            path = "/api/v1/integration/analyses?limit=1"
            try:
                with self.assertRaises(TransportFailure) as untrusted:
                    self.client(url=url).json("GET", path)
                self.assertEqual(untrusted.exception.category, "certificate_rejected")
                self.assertEqual(server.received, [])
                with patch.dict(os.environ, {"SSL_CERT_FILE": str(certificate)}):
                    self.assertEqual(self.client(url=url).json("GET", path), {"ok": True})
                    self.assertEqual(len(server.received), 1)
                    self.assertEqual(
                        server.received[0][2]["Authorization"], "Bearer private-server-token"
                    )
                    server.received.clear()
                    # The certificate covers only the numeric IPv4 address.
                    with self.assertRaises(TransportFailure) as mismatch:
                        self.client(url=f"https://localhost:{server.server_port}").json("GET", path)
                    self.assertEqual(mismatch.exception.category, "certificate_rejected")
                    self.assertEqual(server.received, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_internal_paths_and_claims_cannot_be_injected(self):
        for path in (
            "https://other.example.test",
            "/api/v1/integration/analyses#x",
            "/api/v1/integration/analyses?url=evil",
            "/api/v1/integration/analyses?limit=22&offset=0",
            "/api/v1/integration/analyses?limit=1&offset=10001",
            "/api/v1/integration/analyses?limit=0&offset=0",
            "/api/v1/integration/analyses?limit=1&offset=0&limit=2",
            "/api/v1/integration/snapshots?limit=1&offset=0",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.client().json("GET", path)
        with self.assertRaises(ValueError):
            RegimexClient(self.connection, subject="7\r\nAuthorization: other", run_id="42")
        self.assertEqual(self.server.received, [])

    def test_only_bounded_analysis_history_pages_are_allowed(self):
        for path in (
            "/api/v1/integration/analyses?limit=1",
            "/api/v1/integration/analyses?limit=11&offset=20",
            "/api/v1/integration/analyses?limit=21&offset=10000",
        ):
            self.assertEqual(self.client().json("GET", path), {"ok": True})
        self.assertEqual(len(self.server.received), 3)
