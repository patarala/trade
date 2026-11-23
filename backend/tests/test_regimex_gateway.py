"""Authorization and immutable bindings through the simulator's real HTTP handlers."""

import asyncio
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from pydantic import SecretStr

from backend.app.api.routes import regimex as routes
from backend.app.core.security import create_access_token
from backend.app.db.dependencies import get_db
from backend.app.integrations.regimex.bindings import VerifiedExports
from backend.app.integrations.regimex.delivery import Delivery
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
        self.history_override = {}
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
            "snapshot_sha256": self.export["content_sha256"],
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
                "created_at": "2026-01-01T12:00:00+00:00",
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
        if path.startswith("/api/v1/integration/analyses?"):
            query = parse_qs(urlsplit(path).query)
            limit, offset = int(query["limit"][0]), int(query["offset"][0])
            return {
                "items": copy.deepcopy(list(reversed(self.analyses.values()))[offset:offset + limit]),
                "limit": limit,
                "offset": offset,
                **self.history_override,
            }
        if urlsplit(path).path == "/api/v1/integration/analysis-intake" and method == "POST":
            assert payload == self.export
            if idempotency_key not in self.idempotency:
                retry = parse_qs(urlsplit(path).query).get("retry_of")
                if retry and self.analyses[retry[0]]["job"]["state"] not in {"failed", "cancelled"}:
                    raise GatewayError("retry_not_available", "Retry unavailable.", 409)
                self.idempotency[idempotency_key] = self.create_analysis()["analysis_id"]
            return {
                "snapshot": self.snapshot(),
                "analysis": copy.deepcopy(self.analyses[self.idempotency[idempotency_key]]),
            }
        identifier = path.split("/")[5]
        if identifier not in self.analyses:
            raise GatewayError("not_found", "Analysis was not found.", 404)
        return copy.deepcopy(self.analyses[identifier])

    def delivery(self, identifier, *, include_report):
        path = f"/api/v1/integration/analyses/{identifier}/delivery"
        self.calls.append(("GET", path, None, None))
        analysis = self.analyses.get(identifier)
        if analysis is None:
            raise GatewayError("not_found", "Analysis was not found.", 404)
        if not analysis["result_available"]:
            raise GatewayError("analysis_conflict", "Result is not ready.", 409)
        if include_report and not analysis["report_available"]:
            raise GatewayError("not_found", "Report was not found.", 404)
        report = b"<!doctype html><title>Analysis report</title>" if include_report else None
        result = self.result(identifier)
        return Delivery(copy.deepcopy(analysis), result, json.dumps(result, indent=2).encode(), report)


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
            patch.object(routes, "_verified_exports", VerifiedExports()),
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

    def request(self, method="GET", suffix="", payload=None, *, run_id=1, token=None, query="", **options):
        return asyncio.run(
            request_raw(
                method,
                f"{self.base}{suffix}?run_id={run_id}{query}",
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

    def test_disabled_overview_and_download_agree_for_unsupported_runs(self):
        run = self.fixture.fixture.run
        original = run.configuration_snapshot
        unfrozen = copy.deepcopy(original)
        del unfrozen["instrument_configs"][0]["instrument"]
        with (
            patch.object(routes.settings, "regimex_url", None),
            patch.object(routes.settings, "regimex_token", None),
        ):
            for engine, snapshot in (("1", original), ("3", unfrozen)):
                with self.subTest(engine=engine):
                    run.engine_version = engine
                    run.configuration_snapshot = snapshot
                    self.fixture.db.commit()
                    status, _, overview = self.request()
                    self.assertEqual(status, 200)
                    status, _, error = self.request(suffix="/export")
                    self.assertEqual(status, 422)
                    self.assertEqual(
                        json.loads(overview),
                        {
                            "enabled": False, "eligible": False,
                            "reason": json.loads(error)["detail"], "analysis": None,
                        },
                    )
        self.assertEqual(self.peer.calls, [])

    def test_offline_overview_offers_an_export_only_after_local_run_checks(self):
        self.peer.failure = GatewayError("analysis_unavailable", "Offline.", 503, retryable=True)
        status, _, content = self.request()
        self.assertEqual(status, 503)
        self.assertIs(json.loads(content)["export_available"], True)
        status, _, content = self.request(suffix="/export")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content), self.export)
        for options in ({"run_id": 999}, {"token": self.other_token}, {"token": "expired"}):
            status, _, content = self.request(**options)
            self.assertIn(status, (401, 404))
            self.assertNotIn("export_available", json.loads(content))
        self.peer.failure = GatewayError("not_found", "Analysis no longer exists.", 404)
        status, _, content = self.request(query=f"&analysis_id={uuid4()}")
        self.assertEqual(status, 404)
        self.assertNotIn("export_available", json.loads(content))

    def test_submit_duplicate_reload_and_report_keep_one_analysis(self):
        status, _, content = self.request()
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(content)["analysis"])
        before = len(self.peer.calls)
        first, repeated = self.submit(), self.submit()
        self.assertEqual(len(self.peer.calls) - before, 2)
        self.assertEqual(self.peer.calls[-1], self.peer.calls[-2])
        self.assertEqual(self.peer.calls[-1][1], "/api/v1/integration/analysis-intake")
        self.assertEqual(first["analysis_id"], repeated["analysis_id"])
        self.assertEqual(len(self.peer.analyses), 1)
        self.assertEqual(first["run_id"], 1)
        self.peer.complete(first["analysis_id"])
        status, _, content = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["analysis"]["state"], "succeeded")
        before = len(self.peer.calls)
        status, headers, content = self.request(suffix=f"/analyses/{first['analysis_id']}/report")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.peer.calls) - before, 1)
        self.assertIn(b"attachment", headers[b"content-disposition"])
        self.assertIn(b"sandbox", headers[b"content-security-policy"])
        self.assertIn(b"Analysis report", content)
        self.assertTrue(all(identity == ("1", "1") for identity in self.peer.identities))

    def test_history_pages_include_verified_model_dates_and_older_report_access(self):
        identifiers = [self.peer.create_analysis()["analysis_id"] for _ in range(3)]
        self.peer.complete(identifiers[0])
        before = len(self.peer.calls)
        status, headers, content = self.request(suffix="/analyses", query="&limit=2&offset=0")
        self.assertEqual(status, 200, content)
        page = json.loads(content)
        self.assertEqual([item["analysis_id"] for item in page["items"]], identifiers[:0:-1])
        self.assertEqual((page["limit"], page["offset"], page["has_more"]), (2, 0, True))
        self.assertEqual(page["items"][0]["model_version_id"], "model-1")
        self.assertEqual(page["items"][0]["created_at"], "2026-01-01T12:00:00+00:00")
        self.assertIn(b"no-store", headers[b"cache-control"])
        self.assertEqual(len(self.peer.calls) - before, 1)
        status, _, content = self.request(suffix="/analyses", query="&limit=2&offset=2")
        self.assertEqual(status, 200)
        page = json.loads(content)
        self.assertEqual([item["analysis_id"] for item in page["items"]], identifiers[:1])
        self.assertFalse(page["has_more"])
        self.assertEqual(self.request(suffix=f"/analyses/{identifiers[0]}/report")[0], 200)

    def test_saved_analysis_without_display_metadata_remains_bound_to_its_export(self):
        old = self.peer.create_analysis()
        identifier = old["analysis_id"]
        old.update(instrument_ids=[], step_count=None)
        self.peer.complete(identifier)
        recent = self.peer.create_analysis()
        status, _, content = self.request(suffix="/analyses")
        self.assertEqual(status, 200, content)
        self.assertEqual([item["analysis_id"] for item in json.loads(content)["items"]],
                         [recent["analysis_id"], identifier])
        preview = f"/analyses/{identifier}/preview"
        status, _, content = self.request(suffix=preview)
        self.assertEqual(status, 200, content)
        self.assertEqual(json.loads(content)["input"], self.export)

        for field, value in (("time_grid", {}), ("instruments", []), ("snapshot_sha256", "f" * 64)):
            with self.subTest(field=field):
                self.peer.result_override = {field: value}
                self.assertEqual(self.request(suffix=preview)[0], 502)
        self.peer.result_override = {}
        old["snapshot_sha256"] = "0" * 64
        self.assertEqual(self.request(suffix="/analyses")[0], 502)
        self.assertEqual(self.request(suffix=preview)[0], 502)

    def test_missing_or_partial_display_metadata_is_invalid(self):
        item = self.peer.create_analysis()
        for metadata in ({"instrument_ids": []}, {"step_count": None},
                         {"instrument_ids": [], "step_count": 1},
                         {"instrument_ids": item["instrument_ids"], "step_count": None}):
            with self.subTest(metadata=metadata):
                item.pop("instrument_ids", None)
                item.pop("step_count", None)
                item.update(metadata)
                self.assertEqual(self.request(suffix="/analyses")[0], 502)

    def test_intake_response_must_bind_the_exact_snapshot_to_the_accepted_job(self):
        for field, wrong in (
            ("snapshot_id", str(uuid4())), ("content_sha256", "f" * 64),
            ("run_id", "2"), ("source_id", "other"), ("source_snapshot_id", "wrong"),
        ):
            self.peer.snapshot_override = {field: wrong}
            with self.subTest(field=field):
                self.assertEqual(self.request("POST", "/analyses", {})[0], 502)
        self.assertEqual(len(self.peer.analyses), 1)

    def test_history_rejects_wrong_bindings_duplicate_ids_and_invalid_pages(self):
        item = self.peer.create_analysis()
        for replacement in (
            {"limit": 10}, {"offset": 1}, {"items": [item, item]},
            {"items": [{**item, "run_id": "2"}]},
            {"items": [{**item, "source_id": "other-source"}]},
            {"items": [{**item, "job": {**item["job"], "created_at": "private diagnostic"}}]},
            {"items": [{**item, "job": {**item["job"], "created_at": "2026-01-01T00:00:00"}}]},
        ):
            with self.subTest(replacement=replacement):
                self.peer.history_override = replacement
                status, _, content = self.request(suffix="/analyses")
                self.assertEqual(status, 502, content)
                self.assertNotIn(item["analysis_id"].encode(), content)
                self.assertNotIn(b"private diagnostic", content)
        self.peer.history_override = {}
        item["snapshot_sha256"] = "0" * 64
        self.assertEqual(self.request(suffix="/analyses")[0], 502)

    def test_opening_an_older_analysis_uses_one_bound_request(self):
        selected = self.peer.create_analysis()["analysis_id"]
        self.peer.create_analysis()
        query = "&analysis_id=" + selected
        self.assertEqual(self.request(query=query, token=self.other_token)[0], 404)
        self.assertEqual(self.request(query="&analysis_id=bad")[0], 422)
        self.assertFalse(self.peer.calls)
        status, _, body = self.request(query=query)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["analysis"]["analysis_id"], selected)
        self.assertEqual(len(self.peer.calls), 1)
        self.assertEqual(self.peer.calls[0][1], "/api/v1/integration/analyses/" + selected)
        self.peer.analyses[selected]["snapshot_sha256"] = "0" * 64
        self.assertEqual(self.request(query=query)[0], 502)

    def test_history_limits_are_checked_before_upstream_requests(self):
        for query in ("&limit=0", "&limit=21", "&offset=-1", "&offset=10001", "&offset=invalid"):
            with self.subTest(query=query):
                self.assertEqual(self.request(suffix="/analyses", query=query)[0], 422)
        self.assertEqual(self.peer.calls, [])

    def test_link_resolves_only_the_requested_owned_run_without_calling_regimex(self):
        with patch.object(routes.settings, "regimex_url", None):
            status, headers, content = self.request(suffix="/run")
        self.assertEqual(status, 200)
        run = json.loads(content)
        self.assertEqual((run["id"], run["simulation_id"], run["status"]), (1, 1, "completed"))
        self.assertTrue(run["instrument_ids"])
        self.assertIn(b"no-store", headers[b"cache-control"])
        self.assertEqual(self.peer.calls, [])

    def test_every_route_checks_user_and_explicit_run_before_contacting_regimex(self):
        identifier = str(uuid4())
        for method, suffix, body in (
            ("GET", "", None),
            ("GET", "/export", None),
            ("GET", "/run", None),
            ("GET", "/analyses", None),
            ("POST", "/analyses", {}),
            ("GET", f"/analyses/{identifier}", None),
            ("GET", f"/analyses/{identifier}/report", None),
            ("GET", f"/analyses/{identifier}/result", None),
            ("GET", f"/analyses/{identifier}/preview", None),
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
        self.assertEqual(self.request(suffix=f"/analyses/{first['analysis_id']}")[0], 200)
        user = self.fixture.db.get(User, 1)
        user.is_active = False
        self.fixture.db.commit()
        before = len(self.peer.calls)
        self.assertEqual(self.request(suffix=f"/analyses/{first['analysis_id']}")[0], 401)
        self.assertEqual(self.request(suffix=f"/analyses/{first['analysis_id']}/report")[0], 401)
        self.assertEqual(self.request(suffix=f"/analyses/{first['analysis_id']}/preview")[0], 401)
        self.assertEqual(len(self.peer.calls), before)

    def test_wrong_snapshot_or_run_never_releases_a_report(self):
        first = self.submit()
        identifier = first["analysis_id"]
        self.peer.complete(identifier)
        for field, wrong in (
            ("source_id", "another-source"),
            ("run_id", "2"),
            ("snapshot_sha256", "0" * 64),
            ("source_snapshot_id", "another-snapshot"),
        ):
            original = self.peer.analyses[identifier][field]
            self.peer.analyses[identifier][field] = wrong
            with self.subTest(field=field):
                status, _, body = self.request(suffix=f"/analyses/{identifier}/report")
                self.assertEqual(status, 502)
                self.assertNotIn(b"Analysis report", body)
            self.peer.analyses[identifier][field] = original
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
            with self.subTest(fields=list(replacement)):
                self.assertEqual(self.request(suffix=f"/analyses/{identifier}/result")[0], 502)
                status, _, body = self.request(suffix=f"/analyses/{identifier}/report")
                self.assertEqual(status, 502)
                self.assertNotIn(b"Analysis report", body)

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
        before = len(self.peer.calls)
        retried = self.submit({"retry_of": identifier})
        repeated = self.submit({"retry_of": identifier})
        self.assertEqual(len(self.peer.calls) - before, 2)
        self.assertEqual(self.peer.calls[-1], self.peer.calls[-2])
        self.assertEqual(
            self.peer.calls[-1][1], f"/api/v1/integration/analysis-intake?retry_of={identifier}"
        )
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


    def test_result_download_preserves_the_verified_member_bytes(self):
        identifier = self.submit()["analysis_id"]
        self.peer.complete(identifier)
        status, _, content = self.request(suffix=f"/analyses/{identifier}/result")
        self.assertEqual(status, 200)
        self.assertEqual(content, json.dumps(self.peer.result(identifier), indent=2).encode())

    def test_preview_builds_one_export_and_preserves_verified_identity(self):
        analysis = self.submit()
        identifier = analysis["analysis_id"]
        self.peer.complete(identifier)
        with patch.object(routes, "build_export", wraps=build_export) as build:
            status, headers, content = self.request(suffix=f"/analyses/{identifier}/preview")
        self.assertEqual(status, 200, content)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(json.loads(content), {"result": self.peer.result(identifier), "input": self.export})
        self.assertEqual(headers[b"content-type"], b"application/json")
        self.assertIn(b"no-store", headers[b"cache-control"])
        self.assertEqual(self.request(suffix=f"/analyses/{identifier}/preview", token=self.other_token)[0], 404)
        self.peer.analyses[identifier]["snapshot_sha256"] = "f" * 64
        self.assertEqual(self.request(suffix=f"/analyses/{identifier}/preview")[0], 502)

    def test_preview_bounds_combined_response_before_publication(self):
        identifier = self.submit()["analysis_id"]
        self.peer.complete(identifier)
        with patch.object(routes, "_MAX_PREVIEW_BYTES", 16):
            status, _, content = self.request(suffix=f"/analyses/{identifier}/preview")
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(content)["code"], "preview_too_large")

    def test_metadata_reads_reuse_one_verified_digest_but_downloads_rebuild(self):
        identifier = self.submit()["analysis_id"]
        self.peer.complete(identifier)
        with patch.object(routes, "build_export", wraps=build_export) as build:
            for suffix in ("", "/analyses", f"/analyses/{identifier}", f"/analyses/{identifier}"):
                self.assertEqual(self.request(suffix=suffix)[0], 200)
            self.assertEqual(build.call_count, 1)
            for suffix in (f"/analyses/{identifier}/preview", f"/analyses/{identifier}/result",
                           f"/analyses/{identifier}/report", "/export"):
                self.assertEqual(self.request(suffix=suffix)[0], 200)
            self.assertEqual(build.call_count, 5)
        self.peer.analyses[identifier]["snapshot_sha256"] = "f" * 64
        for suffix in ("", "/analyses", f"/analyses/{identifier}"):
            self.assertEqual(self.request(suffix=suffix)[0], 502)
        self.assertEqual(self.request(suffix=f"/analyses/{identifier}", token=self.other_token)[0], 404)

    def test_failed_remote_binding_is_not_cached(self):
        identifier = self.submit()["analysis_id"]
        self.peer.analyses[identifier]["snapshot_sha256"] = "f" * 64
        with patch.object(routes, "build_export", wraps=build_export) as build:
            for _ in range(2):
                self.assertEqual(self.request(suffix=f"/analyses/{identifier}")[0], 502)
            self.assertEqual(build.call_count, 2)

    def test_model_identifiers_follow_shared_unicode_code_point_contract(self):
        identifier = self.submit()["analysis_id"]
        fixtures = json.loads((Path(__file__).parents[2] / "tests/contracts/identifiers.json").read_text())
        for fixture in fixtures:
            with self.subTest(fixture["name"]):
                self.peer.analyses[identifier]["job"]["model"]["model_version_id"] = fixture["value"]
                status, _, content = self.request(suffix=f"/analyses/{identifier}")
                self.assertEqual(status, 200 if fixture["valid"] else 502, content)

    def test_cached_digest_expires_and_changed_run_metadata_forces_validation(self):
        now = [0.0]
        identifier = self.submit()["analysis_id"]
        with (
            patch.object(routes, "_verified_exports", VerifiedExports(clock=lambda: now[0])),
            patch.object(routes, "build_export", wraps=build_export) as build,
        ):
            self.assertEqual(self.request(suffix=f"/analyses/{identifier}")[0], 200)
            now[0] = 59
            self.assertEqual(self.request(suffix=f"/analyses/{identifier}")[0], 200)
            self.assertEqual(build.call_count, 1)
            now[0] = 60
            self.assertEqual(self.request(suffix=f"/analyses/{identifier}")[0], 200)
            self.assertEqual(build.call_count, 2)
            run = self.fixture.fixture.run
            run.engine_version = "unsupported"
            self.fixture.db.commit()
            self.assertEqual(self.request(suffix=f"/analyses/{identifier}")[0], 422)
            self.assertEqual(build.call_count, 3)


    def test_changed_source_or_frozen_price_cannot_reuse_cached_identity(self):
        identifier = self.submit()["analysis_id"]
        self.assertEqual(self.request(suffix=f"/analyses/{identifier}")[0], 200)
        with patch.object(routes, "build_export", wraps=build_export) as build:
            with patch.object(routes.settings, "regimex_source_id", "different-source"):
                self.assertEqual(self.request(suffix=f"/analyses/{identifier}")[0], 502)
            self.assertEqual(build.call_count, 1)
            # Even an unsupported direct frozen-metadata edit invalidates the key.
            market = self.fixture.fixture.run.instruments[0]
            market.initial_price += 1
            self.fixture.db.commit()
            self.assertEqual(self.request(suffix=f"/analyses/{identifier}")[0], 502)
            self.assertEqual(build.call_count, 2)
