"""Slow optional analysis cannot consume all simulator HTTP worker capacity."""

import asyncio
import json
import threading
import unittest
from unittest.mock import patch

import anyio.to_thread
from fastapi import FastAPI

from backend.app.api.routes import regimex, simulation_results
from backend.app.db.dependencies import get_db
from backend.app.integrations.regimex.errors import GatewayError
from backend.app.integrations.regimex.limiter import RegimexConcurrencyMiddleware
from backend.tests import test_regimex_gateway as gateway_tests


class RegimexAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = gateway_tests.RegimexGatewayTests()
        self.fixture.setUp()
        self.application = FastAPI()
        self.application.include_router(regimex.router, prefix="/api")
        self.application.include_router(simulation_results.router, prefix="/api")
        self.application.add_exception_handler(GatewayError, regimex.gateway_error)
        self.application.add_middleware(RegimexConcurrencyMiddleware, max_concurrent_requests=1)
        self.database_calls = 0

        def database():
            self.database_calls += 1
            with self.fixture.fixture.factory() as session:
                yield session

        self.application.dependency_overrides[get_db] = database
        self.application_patch = patch.object(gateway_tests, "app", self.application)
        self.application_patch.start()

    def tearDown(self):
        self.application_patch.stop()
        self.fixture.tearDown()

    async def request(self, path="/api/simulations/1/regimex?run_id=1"):
        return await gateway_tests.request_raw("GET", path, self.fixture.token)

    async def test_saturation_is_rejected_before_database_or_export_and_results_stay_available(
        self,
    ):
        started, release = threading.Event(), threading.Event()
        original = self.fixture.peer.json

        def slow(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise AssertionError("The test did not release the simulated upstream")
            return original(*args, **kwargs)

        threads = anyio.to_thread.current_default_thread_limiter()
        previous = threads.total_tokens
        threads.total_tokens = 2
        running = None
        try:
            with (
                patch.object(self.fixture.peer, "json", slow),
                patch.object(
                    regimex,
                    "build_export",
                    wraps=regimex.build_export,
                ) as export,
            ):
                running = asyncio.create_task(self.request())
                async with asyncio.timeout(2):
                    while not started.is_set():
                        await asyncio.sleep(0.01)
                replies = await asyncio.wait_for(
                    asyncio.gather(*(self.request() for _ in range(12))), 1
                )
                for status, headers, body in replies:
                    self.assertEqual(status, 503)
                    self.assertEqual(headers[b"retry-after"], b"2")
                    self.assertEqual(json.loads(body)["code"], "analysis_busy")
                self.assertEqual(self.database_calls, 1)
                self.assertEqual(export.call_count, 1)
                ordinary = await asyncio.wait_for(
                    self.request("/api/simulations/1/results?run_id=1"), 1
                )
                self.assertEqual(ordinary[0], 200, ordinary[2])
                self.assertEqual(self.database_calls, 2)
                self.assertEqual(export.call_count, 1)
                release.set()
                self.assertEqual((await asyncio.wait_for(running, 2))[0], 200)
            self.assertEqual((await self.request())[0], 200)
        finally:
            release.set()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            threads.total_tokens = previous

    async def test_upstream_failure_and_handler_exception_release_the_slot(self):
        self.fixture.peer.failure = GatewayError("analysis_unavailable", "Unavailable", 503)
        self.assertEqual((await self.request())[0], 503)
        self.fixture.peer.failure = None
        self.assertEqual((await self.request())[0], 200)
        with (
            patch.object(self.fixture.peer, "json", side_effect=RuntimeError("Test failure")),
            self.assertRaises(RuntimeError),
        ):
            await self.request()
        self.assertEqual((await self.request())[0], 200)

    async def test_authentication_still_applies_to_admitted_requests(self):
        unauthenticated = await gateway_tests.request_raw(
            "GET", "/api/simulations/1/regimex?run_id=1"
        )
        self.assertEqual(unauthenticated[0], 401)
        other = await gateway_tests.request_raw(
            "GET", "/api/simulations/1/regimex?run_id=1", self.fixture.other_token
        )
        self.assertEqual(other[0], 404)
        self.assertEqual(self.fixture.peer.calls, [])

    async def test_separate_application_instances_have_independent_budgets(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(scope, receive, send):
            entered.set()
            await release.wait()
            await self.application(scope, receive, send)

        first = RegimexConcurrencyMiddleware(slow, max_concurrent_requests=1)
        with patch.object(gateway_tests, "app", first):
            waiting = asyncio.create_task(self.request())
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual((await self.request())[0], 503)
            with patch.object(gateway_tests, "app", self.application):
                self.assertEqual((await self.request())[0], 200)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            release.set()
            self.assertEqual((await self.request())[0], 200)
