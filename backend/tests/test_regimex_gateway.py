"""Authorization and immutable bindings through the simulator's real HTTP handlers."""

import asyncio
import copy
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import SecretStr

from backend.app.api.routes import regimex as routes
from backend.app.core.security import create_access_token
from backend.app.db.dependencies import get_db
from backend.app.integrations.regimex.client import UpstreamResponse
from backend.app.integrations.regimex.errors import GatewayError
from backend.app.integrations.regimex.export import build_export
from backend.app.main import app
from backend.app.models import User
from backend.tests import test_simulation_execution as execution_fixture
from backend.tests.test_regimex_export import freeze_instruments


async def request_raw(
    method, url, token=None, payload=None, *, headers=(), raw_body=None, receive_override=None
):
    parts = urlsplit(url)
    request_headers = list(headers)
    if token is not None:
        request_headers.append((b"authorization", f"Bearer {token}".encode()))
    body = (
        raw_body
        if raw_body is not None
        else b""
        if payload is None
        else json.dumps(payload).encode()
    )
    if payload is not None or raw_body is not None:
        request_headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": parts.path,
        "raw_path": parts.path.encode(),
        "query_string": parts.query.encode(),
        "root_path": "",
        "headers": request_headers,
        "server": ("test", 80),
        "client": ("test", 1234),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive_override or receive, send)
    response = next(message for message in messages if message["type"] == "http.response.start")
    content = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return response["status"], dict(response["headers"]), content


class AnalysisPeer:
    def __init__(self, export):
        self.export = export
        self.subject = "1"
        self.snapshot_id = str(uuid4())
        self.analyses, self.idempotency = {}, {}
        self.latest_id = None
        self.calls, self.identities = [], []
        self.snapshot_override = {}
        self.result_override = {}
        self.failure = None

    def connect(self, connection, *, subject, run_id):
        self.identities.append((subject, run_id))
        self.subject = subject
        return self

    def snapshot(self):
        source = self.export["source"]
        return {
            "snapshot_id": self.snapshot_id,
            "source_id": source["source_id"],
            "run_id": source["run_id"],
            "source_snapshot_id": source["snapshot_id"],
            "content_sha256": self.export["content_sha256"],
            **self.snapshot_override,
        }

    def create_analysis(self):
        identifier, source = str(uuid4()), self.export["source"]
        value = {
            "analysis_id": identifier,
            "snapshot_id": self.snapshot_id,
            "source_id": source["source_id"],
            "run_id": source["run_id"],
            "source_snapshot_id": source["snapshot_id"],
            "instrument_ids": [item["instrument_id"] for item in self.export["instruments"]],
            "step_count": self.export["time_grid"]["step_count"],
            "result_available": False,
            "report_available": False,
            "job": {
                "operation_id": str(uuid4()),
                "kind": "analysis",
                "resource_id": identifier,
                "state": "queued",
                "stage": "queued",
                "failure": None,
                "model": {
                    "model_version_id": "model-1",
                    "bundle_sha256": "a" * 64,
                    "inference_policy_sha256": "b" * 64,
                },
            },
        }
        self.analyses[identifier] = value
        self.latest_id = identifier
        return value

    def complete(self, identifier):
        value = self.analyses[identifier]
        value["job"]["state"], value["job"]["stage"] = "succeeded", "complete"
        value["result_available"] = value["report_available"] = True

    def result(self, identifier):
        count = self.export["time_grid"]["step_count"]
        result = {
            "result_version": "2.0",
            "analysis_id": identifier,
            "snapshot_id": self.snapshot_id,
            "snapshot_sha256": self.export["content_sha256"],
            "source": self.export["source"],
            "time_grid": self.export["time_grid"],
            "instruments": self.export["instruments"],
            "model": self.analyses[identifier]["job"]["model"],
            "class_names": ["sideways", "growth", "decline"],
            "sequences": [
                {
                    "instrument_id": item["instrument_id"],
                    "probabilities": [[1.0, 0.0, 0.0]] * count,
                    "intervals": [
                        {
                            "instrument_id": item["instrument_id"],
                            "start": 0,
                            "stop": count,
                            "direction": "sideways",
                            "status": "observed",
                        }
                    ],
                }
                for item in self.export["instruments"]
            ],
        }
        return {**result, **self.result_override}

    def json(self, method, path, *, payload=None, idempotency_key=None):
        self.calls.append((method, path, payload, idempotency_key))
        if self.failure is not None:
            raise self.failure
        if path == "/api/v1/integration/analyses?limit=1":
            return {
                "items": []
                if self.latest_id is None
                else [copy.deepcopy(self.analyses[self.latest_id])],
                "limit": 1,
                "offset": 0,
            }
        if (
            path == "/api/v1/integration/snapshots"
            or path == f"/api/v1/integration/snapshots/{self.snapshot_id}"
        ):
            return self.snapshot()
        if path == "/api/v1/integration/analyses" and method == "POST":
            if idempotency_key not in self.idempotency:
                self.idempotency[idempotency_key] = self.create_analysis()["analysis_id"]
            return copy.deepcopy(self.analyses[self.idempotency[idempotency_key]])
        identifier = path.split("/")[5]
        if identifier not in self.analyses:
            raise GatewayError("not_found", "Analysis was not found.", 404)
        return (
            self.result(identifier)
            if path.endswith("/result")
            else copy.deepcopy(self.analyses[identifier])
        )

    def request(self, method, path):
        self.calls.append((method, path, None, None))
        return UpstreamResponse(
            "text/html; charset=utf-8", b"<!doctype html><title>Analysis report</title>"
        )


class RegimexGatewayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = execution_fixture.SimulationExecutionTests()
        self.fixture.setUp()
        freeze_instruments(self.fixture.db, self.fixture.fixture.run)
        self.fixture.db.add(
            User(id=2, email="other@example.test", hashed_password="unused", role="admin")
        )
        self.fixture.db.commit()
        self.fixture.run_all()
        self.export = build_export(self.fixture.db, self.fixture.fixture.run, "trade-test")
        self.peer = AnalysisPeer(self.export)

        def test_db():
            with self.fixture.factory() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        self.token, self.other_token = create_access_token("1"), create_access_token("2")
        self.patches = [
            patch.object(routes.settings, "regimex_url", "http://127.0.0.1:8010"),
            patch.object(routes.settings, "regimex_token", SecretStr("private-regimex-token")),
            patch.object(routes.settings, "regimex_source_id", "trade-test"),
            patch.object(routes, "RegimexClient", self.peer.connect),
        ]
        for item in self.patches:
            item.start()
        self.base = "/api/simulations/1/regimex"

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        app.dependency_overrides.clear()
        self.fixture.tearDown()

    def request(self, method="GET", suffix="", payload=None, *, run_id=1, token=None, **options):
        return asyncio.run(
            request_raw(
                method,
                f"{self.base}{suffix}?run_id={run_id}",
                self.token if token is None else token,
                payload,
                **options,
            )
        )

    def submit(self, payload=None):
        status, _, content = self.request("POST", "/analyses", {} if payload is None else payload)
        self.assertEqual(status, 202, content)
        return json.loads(content)

    def test_disabled_integration_makes_no_upstream_requests_but_exports_still_work(self):
        with (
            patch.object(routes.settings, "regimex_url", None),
            patch.object(routes.settings, "regimex_token", None),
        ):
            status, headers, content = self.request()
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(content),
                {"enabled": False, "eligible": True, "reason": None, "analysis": None},
            )
            self.assertIn(b"no-store", headers[b"cache-control"])
            self.assertEqual(self.request("POST", "/analyses", {})[0], 503)
            status, headers, content = self.request(suffix="/export")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(content), self.export)
            self.assertIn(b"attachment", headers[b"content-disposition"])
        self.assertEqual(self.peer.calls, [])

    def test_submit_duplicate_reload_and_report_keep_one_analysis(self):
        status, _, content = self.request()
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(content)["analysis"])
        first, repeated = self.submit(), self.submit()
        self.assertEqual(first["analysis_id"], repeated["analysis_id"])
        self.assertEqual(len(self.peer.analyses), 1)
        self.assertEqual(first["run_id"], 1)
        self.peer.complete(first["analysis_id"])
        status, _, content = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["analysis"]["state"], "succeeded")
        status, headers, content = self.request(suffix=f"/analyses/{first['analysis_id']}/report")
        self.assertEqual(status, 200)
        self.assertIn(b"attachment", headers[b"content-disposition"])
        self.assertIn(b"sandbox", headers[b"content-security-policy"])
        self.assertIn(b"Analysis report", content)
        self.assertTrue(all(identity == ("1", "1") for identity in self.peer.identities))

    def test_every_route_checks_user_and_explicit_run_before_contacting_regimex(self):
        identifier = str(uuid4())
        for method, suffix, body in (
            ("GET", "", None),
            ("GET", "/export", None),
            ("POST", "/analyses", {}),
            ("GET", f"/analyses/{identifier}", None),
            ("GET", f"/analyses/{identifier}/report", None),
            ("GET", f"/analyses/{identifier}/result", None),
        ):
            with self.subTest(method=method, suffix=suffix):
                self.assertEqual(self.request(method, suffix, body, token=self.other_token)[0], 404)
                self.assertEqual(self.request(method, suffix, body, run_id=999)[0], 404)
                unauthenticated = asyncio.run(
                    request_raw(method, f"{self.base}{suffix}?run_id=1", payload=body)
                )
                self.assertEqual(unauthenticated[0], 401)
                missing_run = asyncio.run(request_raw(method, self.base + suffix, self.token, body))
                self.assertEqual(missing_run[0], 422)
        self.assertEqual(self.peer.calls, [])

    def test_browser_identity_assertions_and_duplicate_authorization_are_rejected(self):
        for name in (
            "x-regimex-subject",
            "x-regimex-run-id",
            "x-owner-id",
            "x-source-namespace",
            "authorization",
        ):
            with self.subTest(header=name):
                status, _, _ = self.request(headers=[(name.encode(), b"2")])
                self.assertIn(status, (400, 401))
        self.assertEqual(self.peer.calls, [])

    def test_revoked_user_cannot_poll_or_download_an_existing_analysis(self):
        first = self.submit()
        user = self.fixture.db.get(User, 1)
        user.is_active = False
        self.fixture.db.commit()
        before = len(self.peer.calls)
        self.assertEqual(self.request(suffix=f"/analyses/{first['analysis_id']}")[0], 401)
        self.assertEqual(self.request(suffix=f"/analyses/{first['analysis_id']}/report")[0], 401)
        self.assertEqual(len(self.peer.calls), before)

    def test_wrong_snapshot_or_run_never_releases_a_report(self):
        first = self.submit()
        identifier = first["analysis_id"]
        self.peer.complete(identifier)
        for field, wrong in (
            ("source_id", "another-source"),
            ("run_id", "2"),
            ("content_sha256", "0" * 64),
            ("source_snapshot_id", "another-snapshot"),
        ):
            self.peer.snapshot_override = {field: wrong}
            before = len(self.peer.calls)
            with self.subTest(field=field):
                self.assertEqual(self.request(suffix=f"/analyses/{identifier}/report")[0], 502)
                self.assertFalse(
                    any(call[1].endswith("/report") for call in self.peer.calls[before:])
                )
        self.peer.snapshot_override = {}
        self.peer.analyses[identifier]["run_id"] = "2"
        self.assertEqual(self.request()[0], 502)

    def test_result_identity_and_grid_are_checked_before_html(self):
        first = self.submit()
        identifier = first["analysis_id"]
        self.peer.complete(identifier)
        for replacement in (
            {"snapshot_sha256": "0" * 64},
            {"analysis_id": str(uuid4())},
            {"source": {"run_id": "another-run"}},
            {"sequences": []},
            {"class_names": ["growth", "sideways", "decline"]},
        ):
            self.peer.result_override = replacement
            before = len(self.peer.calls)
            with self.subTest(fields=list(replacement)):
                self.assertEqual(self.request(suffix=f"/analyses/{identifier}/result")[0], 502)
                self.assertEqual(self.request(suffix=f"/analyses/{identifier}/report")[0], 502)
                self.assertFalse(
                    any(call[1].endswith("/report") for call in self.peer.calls[before:])
                )

    def test_failed_retry_is_explicit_and_idempotent(self):
        first = self.submit()
        identifier = first["analysis_id"]
        self.assertEqual(self.request("POST", "/analyses", {"retry_of": identifier})[0], 409)
        previous = self.peer.analyses[identifier]
        previous["job"].update(
            state="failed",
            stage="failed",
            failure={
                "code": "infrastructure_failure",
                "message": "private upstream diagnostic",
                "retryable": True,
            },
        )
        _, _, content = self.request()
        self.assertNotIn(b"private upstream diagnostic", content)
        self.assertEqual(
            json.loads(content)["analysis"]["failure"]["code"], "infrastructure_failure"
        )
        retried = self.submit({"retry_of": identifier})
        repeated = self.submit({"retry_of": identifier})
        self.assertNotEqual(retried["analysis_id"], identifier)
        self.assertEqual(retried, repeated)
        self.assertEqual(len(self.peer.analyses), 2)

    def test_incomplete_upstream_job_is_reported_as_an_invalid_response(self):
        identifier = self.submit()["analysis_id"]
        del self.peer.analyses[identifier]["job"]["failure"]
        self.assertEqual(self.request()[0], 502)
        self.assertEqual(self.request(suffix=f"/analyses/{identifier}")[0], 502)

    def test_result_grid_rejects_gaps_false_probabilities_and_changed_numeric_types(self):
        identifier = self.submit()["analysis_id"]
        self.peer.complete(identifier)
        baseline = self.peer.result(identifier)
        malformed = []
        for probability in ([True, 0, 0], [10**400, 0, 0], [0.2, 0.2, 0.2]):
            sequences = copy.deepcopy(baseline["sequences"])
            sequences[0]["probabilities"][0] = probability
            malformed.append({"sequences": sequences})
        for changes in ({"start": 1}, {"stop": 2}, {"status": "no_trades"}):
            sequences = copy.deepcopy(baseline["sequences"])
            sequences[0]["intervals"][0].update(changes)
            malformed.append({"sequences": sequences})
        malformed.append({"time_grid": {**baseline["time_grid"], "bin_width": True}})
        for replacement in malformed:
            self.peer.result_override = replacement
            with self.subTest(replacement_fields=list(replacement)):
                self.assertEqual(self.request(suffix=f"/analyses/{identifier}/result")[0], 502)

    def test_incomplete_or_oversized_run_is_explicitly_ineligible(self):
        run = self.fixture.fixture.run
        for state, steps in (("stopped", 3), ("failed", 3), ("completed", 4097)):
            run.status, run.total_steps, run.current_step = state, steps, steps
            self.fixture.db.commit()
            with self.subTest(state=state, steps=steps):
                status, _, content = self.request()
                self.assertEqual(status, 200)
                self.assertFalse(json.loads(content)["eligible"])
                self.assertEqual(self.request("POST", "/analyses", {})[0], 409)
                self.assertEqual(self.request(suffix="/export")[0], 409)
        self.assertEqual(self.peer.calls, [])

    def test_request_body_is_bounded_and_rejects_extra_or_duplicate_fields(self):
        for payload in (
            b'{"owner_id":2}',
            b'{"retry_of":null,"retry_of":null}',
            b"[]",
            b'{"retry_of":false}',
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.request("POST", "/analyses", raw_body=payload)[0], 422)
        self.assertEqual(self.request("POST", "/analyses", raw_body=b" " * 4097)[0], 413)
        self.assertEqual(self.peer.calls, [])

    def test_stalled_request_body_times_out_before_opening_the_database(self):
        database_calls, receives = [], []

        def forbidden_db():
            database_calls.append(True)
            raise AssertionError("A stalled body must not acquire a database connection")

        async def stalled_receive():
            self.assertEqual(database_calls, [])
            receives.append(True)
            await asyncio.Event().wait()

        app.dependency_overrides[get_db] = forbidden_db
        with patch.object(routes, "_REQUEST_BODY_TIMEOUT", 0.03):
            status, _, content = asyncio.run(
                asyncio.wait_for(
                    request_raw(
                        "POST",
                        f"{self.base}/analyses?run_id=1",
                        self.token,
                        {},
                        receive_override=stalled_receive,
                    ),
                    timeout=1,
                )
            )
        self.assertEqual(status, 408)
        self.assertEqual(json.loads(content)["code"], "request_timeout")
        self.assertEqual(receives, [True])
        self.assertEqual(database_calls, [])
        self.assertEqual(self.peer.calls, [])

    def test_oversized_declared_body_is_rejected_without_receiving_it(self):
        async def forbidden_receive():
            raise AssertionError("The declared oversized body must not be read")

        status, _, _ = asyncio.run(
            request_raw(
                "POST",
                f"{self.base}/analyses?run_id=1",
                self.token,
                {},
                headers=[(b"content-length", b"4097")],
                receive_override=forbidden_receive,
            )
        )
        self.assertEqual(status, 413)
        self.assertEqual(self.peer.calls, [])

    def test_rate_limit_is_actionable_and_does_not_break_ordinary_results(self):
        self.peer.failure = GatewayError(
            "analysis_busy", "Regimex is busy.", 429, retryable=True, retry_after=8
        )
        status, headers, content = self.request()
        self.assertEqual(status, 429)
        self.assertEqual(headers[b"retry-after"], b"8")
        self.assertTrue(json.loads(content)["retryable"])
        ordinary = asyncio.run(
            request_raw("GET", "/api/simulations/1/results?run_id=1", self.token)
        )
        self.assertEqual(ordinary[0], 200)
