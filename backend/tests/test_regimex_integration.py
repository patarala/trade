"""Opt-in real simulator -> HTTP Regimex -> PostgreSQL -> saved model integration.

Set TRADE_REGIMEX_TEST_DSN to a disposable PostgreSQL database whose test role
can create schemas, and TRADE_REGIMEX_TEST_MODEL to a verified saved model bundle.
Install Regimex with service, ml and viz dependencies in this test environment.
Each invocation owns fresh schemas and object storage; it never uses a running
Regimex installation. This checks interoperability, not classification accuracy.
"""

import hashlib
import json
import os
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from backend.tests.regimex_integration_support import private_workspace, tcp_server

ENABLED = bool(
    os.environ.get("TRADE_REGIMEX_TEST_DSN") and os.environ.get("TRADE_REGIMEX_TEST_MODEL")
)


@unittest.skipUnless(
    ENABLED,
    "Set TRADE_REGIMEX_TEST_DSN and TRADE_REGIMEX_TEST_MODEL for the real integration test.",
)
class RegimexIntegrationTests(unittest.TestCase):
    def test_local_bootstrap_resumes_after_registration_before_activation(self):
        from pydantic import SecretStr
        from regimex.application.service import AnalysisService
        from regimex.service_bootstrap import create_service
        from regimex.service_settings import ServiceSettings

        from backend.scripts.regimex_stack.provision import regimex as provision

        dsn = os.environ["TRADE_REGIMEX_TEST_DSN"]
        model = Path(os.environ["TRADE_REGIMEX_TEST_MODEL"])
        schema = "trade_bootstrap_" + uuid4().hex
        self.addCleanup(self.drop_schema, dsn, schema)
        with private_workspace() as root:
            settings = ServiceSettings(
                database_dsn=SecretStr(dsn), database_schema=schema,
                object_root=root / "objects", work_root=root / "work",
            )
            manifest = (model / "manifest.json").read_bytes()
            state = {
                "source_id": "trade-test", "model_bundle": str(model),
                "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            }
            model_id = json.loads(manifest)["model_version_id"]
            repository = create_service(settings).repository
            with patch.object(ServiceSettings, "from_env", return_value=settings):
                with (
                    patch.object(AnalysisService, "activate_model", side_effect=InterruptedError),
                    self.assertRaises(InterruptedError),
                ):
                    provision(root, state, {})
                self.assertEqual(repository.get_model(model_id).model_version_id, model_id)
                self.assertIsNone(repository.get_active_model())
                provision(root, state, {})
                self.assertEqual(repository.get_active_model().model_version_id, model_id)
                with patch.object(AnalysisService, "activate_model", side_effect=AssertionError):
                    provision(root, state, {})

    @staticmethod
    def drop_schema(dsn, schema):
        import psycopg
        from psycopg import sql

        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )

    def test_real_run_saved_model_report_restart_and_access_boundaries(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict
        from pydantic import SecretStr
        from regimex.api import create_app
        from regimex.application.auth import AuthService
        from regimex.application.service import AnalysisResult, AnalysisService
        from regimex.auth_contracts import AuthPolicy
        from regimex.exports import parse_export_v2
        from regimex.infrastructure.auth_repository import AuthRepository
        from regimex.infrastructure.database import DatabaseRepository
        from regimex.infrastructure.object_store import LocalObjectStore
        from regimex.model_registry import ArchitectureRegistry, InferenceRuntime
        from sqlalchemy import create_engine, func, select
        from sqlalchemy.orm import sessionmaker

        from backend.app.api.routes import regimex as routes
        from backend.app.core.config import settings
        from backend.app.db.base import Base
        from backend.app.db.dependencies import get_db
        from backend.app.integrations.regimex.bindings import VerifiedExports
        from backend.app.integrations.regimex.diagnostics import diagnose
        from backend.app.main import app
        from backend.app.models import Asset, Instrument, ParticipantBalance, User
        from backend.app.services.simulation_execution import process_next_run

        dsn = os.environ["TRADE_REGIMEX_TEST_DSN"]
        model = Path(os.environ["TRADE_REGIMEX_TEST_MODEL"]).resolve(strict=True)
        suffix = uuid4().hex
        trade_schema, regimex_schema = "trade_test_" + suffix, "regimex_trade_test_" + suffix
        with ExitStack() as stack:
            root = stack.enter_context(private_workspace())
            stack.enter_context(patch.object(routes, "_verified_exports", VerifiedExports()))
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(trade_schema)))

            stack.callback(self.drop_schema, dsn, trade_schema)
            stack.callback(self.drop_schema, dsn, regimex_schema)
            parameters = conninfo_to_dict(dsn)
            parameters["options"] = f"-csearch_path={trade_schema} -cstatement_timeout=15000"
            engine = create_engine("postgresql+psycopg://", connect_args=parameters)
            stack.callback(engine.dispose)
            Base.metadata.create_all(engine)
            factory = sessionmaker(bind=engine, autoflush=False)

            def test_db():
                with factory() as db:
                    yield db

            previous_overrides = dict(app.dependency_overrides)
            app.dependency_overrides[get_db] = test_db

            def restore_overrides():
                app.dependency_overrides.clear()
                app.dependency_overrides.update(previous_overrides)

            stack.callback(restore_overrides)
            repository = DatabaseRepository(dsn, schema=regimex_schema)
            repository.migrate()
            stack.enter_context(repository.queue_app.open())
            service = AnalysisService(
                repository,
                LocalObjectStore(
                    root / "objects", max_object_bytes=64 * 1024**2, max_total_bytes=512 * 1024**2
                ),
                ArchitectureRegistry.discover(),
                root / "work",
            )
            admin = repository.authenticate(repository.create_token("test-admin", "admin"))
            manifest_sha256 = hashlib.sha256((model / "manifest.json").read_bytes()).hexdigest()
            active_model = service.register_bundle(
                admin,
                model,
                manifest_sha256=manifest_sha256,
                inference_runtime=InferenceRuntime(threads=1),
                activate=True,
            )
            source = "trade-integration-test"
            integration_token = repository.create_token(
                "trade-test-backend",
                "integration",
                source_namespace=source,
            )
            auth_repository = AuthRepository(repository)

            def regimex_app(origin):
                auth = AuthService(
                    auth_repository,
                    AuthPolicy(public_origin=origin, allow_loopback_http=True),
                )
                return create_app(service, auth_service=auth)

            _regimex, regimex_origin = stack.enter_context(tcp_server(regimex_app))
            stack.enter_context(
                patch.multiple(
                    settings,
                    regimex_url=regimex_origin,
                    regimex_token=SecretStr(integration_token),
                    regimex_source_id=source,
                    regimex_allow_loopback_http=True,
                    jwt_secret=SecretStr("integration-only-signing-secret-0123456789"),
                    password_pepper=SecretStr("integration-only-password-pepper"),
                )
            )
            report = diagnose(settings)
            self.assertEqual(report.status, "passed", report.checks)
            with factory.begin() as db:
                usd, btc, eth = (
                    Asset(symbol="USD", name="US dollar"),
                    Asset(symbol="BTC", name="Bitcoin"),
                    Asset(symbol="ETH", name="Ethereum"),
                )
                db.add_all((usd, btc, eth))
                db.flush()
                markets = [
                    Instrument(base_asset_id=asset.id, quote_asset_id=usd.id)
                    for asset in (btc, eth)
                ]
                db.add_all(markets)
                db.flush()
                instrument_ids, usd_id = [market.id for market in markets], usd.id

            def balances():
                with factory() as db:
                    return dict(
                        db.execute(
                            select(
                                ParticipantBalance.asset_id,
                                func.sum(
                                    ParticipantBalance.available + ParticipantBalance.reserved
                                ),
                            ).group_by(ParticipantBalance.asset_id)
                        ).all()
                    )

            with tcp_server(lambda origin: app) as (browser, _origin):
                alice_id, alice = self.register(browser, "alice@example.com")
                _bob_id, bob = self.register(browser, "bob@example.com")
                config = self.configuration(instrument_ids)
                created = browser.request("POST", "/api/simulations", token=alice, payload=config)
                self.assertEqual(created.status, 201, created.body)
                simulation_id = created.json()["id"]
                base = f"/api/simulations/{simulation_id}"
                started = browser.request("POST", base + "/start", token=alice)
                self.assertEqual(started.status, 201, started.body)
                run_id = started.json()["id"]
                path = base + "/regimex"
                query = f"?run_id={run_id}"
                blocked = browser.request(
                    "POST", path + "/analyses" + query, token=alice, payload={}
                )
                self.assertEqual(blocked.status, 409)
                before_balances = balances()
                for _ in range(config["duration_steps"] + 1):
                    if not process_next_run(factory):
                        break
                else:
                    self.fail("The simulator did not finish within its declared step count.")
                self.assertEqual(before_balances, balances())
                results = browser.request("GET", base + "/results" + query, token=alice)
                self.assertEqual(results.status, 200, results.body)
                self.assertEqual(results.json()["status"], "completed")
                self.assertGreater(results.json()["trade_count"], 0)
                exported = browser.request("GET", path + "/export" + query, token=alice)
                self.assertEqual(exported.status, 200, exported.body)
                export = parse_export_v2(exported.body)
                self.assertEqual(export.time_grid.step_count, config["duration_steps"])
                self.assertEqual(len(export.observations), 2 * config["duration_steps"])
                self.assertEqual(export.instruments[0].initial_price, 100.01)
                self.assertTrue(
                    all(row.bid_state == row.ask_state == "unknown" for row in export.observations)
                )
                with factory.begin() as db:
                    db.get(Asset, usd_id).symbol = "RENAMED"
                repeated = browser.request("GET", path + "/export" + query, token=alice)
                self.assertEqual(repeated.status, 200, repeated.body)
                self.assertEqual(repeated.body, exported.body)

                overview = browser.request("GET", path + query, token=alice)
                self.assertEqual(overview.status, 200, overview.body)
                self.assertIsNone(overview.json()["analysis"])
                accepted = browser.request(
                    "POST", path + "/analyses" + query, token=alice, payload={}
                )
                self.assertEqual(accepted.status, 202, accepted.body)
                analysis_id = accepted.json()["analysis_id"]
                analysis_path = path + "/analyses/" + analysis_id
                replay = browser.request(
                    "POST", path + "/analyses" + query, token=alice, payload={}
                )
                self.assertEqual(replay.status, 202, replay.body)
                self.assertEqual(replay.json()["analysis_id"], analysis_id)
                self.assertEqual(
                    browser.request(
                        "GET",
                        analysis_path + "/report" + query,
                        token=alice,
                    ).status,
                    409,
                )
                principal = auth_repository.bind_subject(
                    integration_token, str(alice_id), str(run_id)
                )
                record = service.get_analysis(principal, UUID(analysis_id))
                completed = service.run_analysis(record.operation.operation_id)
                self.assertIsNotNone(completed)
                self.assertEqual(completed.state, "succeeded", completed.failure)
                status = browser.request("GET", analysis_path + query, token=alice)
                self.assertEqual(status.status, 200, status.body)
                self.assertEqual(status.json()["state"], "succeeded")
                result = browser.request("GET", analysis_path + "/result" + query, token=alice)
                self.assertEqual(result.status, 200, result.body)
                self.assertEqual(hashlib.sha256(result.body).hexdigest(), completed.result_sha256)
                parsed_result = AnalysisResult.model_validate_json(result.body)
                self.assertEqual(parsed_result.snapshot_sha256, export.content_sha256)
                self.assertEqual(parsed_result.source, export.source)
                self.assertEqual(
                    parsed_result.model.model_version_id, active_model.model_version_id
                )
                self.assertEqual(parsed_result.time_grid, export.time_grid)
                self.assertEqual(parsed_result.instruments, export.instruments)
                preview = browser.request("GET", analysis_path + "/preview" + query, token=alice)
                self.assertEqual(preview.status, 200, preview.body[:200])
                pair = preview.json()
                self.assertEqual(set(pair), {"result", "input"})
                self.assertEqual(pair["result"], result.json())
                self.assertEqual(pair["input"], exported.json())
                preview_export = parse_export_v2(json.dumps(pair["input"]).encode())
                preview_result = AnalysisResult.model_validate_json(json.dumps(pair["result"]))
                self.assertEqual(preview_result.snapshot_sha256, preview_export.content_sha256)
                self.assertEqual(preview_result.source, preview_export.source)
                self.assertEqual(preview_result.time_grid, preview_export.time_grid)
                self.assertEqual(preview_result.instruments, preview_export.instruments)
                self.assertEqual(str(preview_result.analysis_id), analysis_id)
                self.assertLessEqual(len(preview.body), 16 * 1024**2)
                self.assertEqual(preview.headers["content-type"], "application/json")
                self.assertIn("no-store", preview.headers["cache-control"])
                report = browser.request("GET", analysis_path + "/report" + query, token=alice)
                self.assertEqual(report.status, 200, report.body[:200])
                self.assertIn(b"<!doctype html", report.body.lower())
                self.assertTrue(report.headers["content-disposition"].startswith("attachment;"))
                self.assertIn("sandbox", report.headers["content-security-policy"])
                for endpoint in (
                    path,
                    path + "/export",
                    analysis_path,
                    analysis_path + "/result",
                    analysis_path + "/preview",
                    analysis_path + "/report",
                ):
                    self.assertEqual(browser.request("GET", endpoint + query).status, 401)
                    self.assertEqual(
                        browser.request("GET", endpoint + query, token=bob).status, 404
                    )
                denied = browser.request(
                    "GET",
                    analysis_path + query,
                    token=alice,
                    headers={"X-Regimex-Subject": str(alice_id)},
                )
                self.assertEqual(denied.status, 400)
                rerun = browser.request("POST", base + "/start", token=alice)
                self.assertEqual(rerun.status, 201, rerun.body)
                other_run = rerun.json()["id"]
                for _ in range(config["duration_steps"] + 1):
                    if not process_next_run(factory):
                        break
                else:
                    self.fail("The second run did not finish within its declared step count.")
                other_query = f"?run_id={other_run}"
                self.assertEqual(
                    browser.request("GET", analysis_path + other_query, token=alice).status, 404
                )
                self.assertEqual(
                    browser.request(
                        "GET", analysis_path + "/preview" + other_query, token=alice
                    ).status,
                    404,
                )
                with factory.begin() as db:
                    db.get(User, alice_id).is_active = False
                self.assertEqual(
                    browser.request("GET", analysis_path + query, token=alice).status, 401
                )
                with factory.begin() as db:
                    db.get(User, alice_id).is_active = True

            # A new HTTP server and new per-request gateway objects recover state
            # from Regimex after discarding the optional local export identities.
            routes._verified_exports = VerifiedExports()
            with tcp_server(lambda origin: app) as (fresh_browser, _origin):
                overview = fresh_browser.request("GET", path + query, token=alice)
                self.assertEqual(overview.status, 200, overview.body)
                self.assertEqual(overview.json()["analysis"]["analysis_id"], analysis_id)
                replay = fresh_browser.request(
                    "POST", path + "/analyses" + query, token=alice, payload={}
                )
                self.assertEqual(replay.status, 202, replay.body)
                self.assertEqual(replay.json()["analysis_id"], analysis_id)
                downloaded = fresh_browser.request(
                    "GET", analysis_path + "/report" + query, token=alice
                )
                self.assertEqual(downloaded.status, 200, downloaded.body[:200])
                self.assertEqual(downloaded.body, report.body)
                with repository.connection() as connection:
                    self.assertEqual(
                        connection.execute("SELECT count(*) FROM regimex_analyses").fetchone()[0], 1
                    )

    def register(self, browser, email):
        credentials = {"email": email, "password": "deliberate-integration-test-password"}
        registered = browser.request("POST", "/api/auth/register", payload=credentials)
        self.assertEqual(registered.status, 201, registered.body)
        logged_in = browser.request("POST", "/api/auth/login", payload=credentials)
        self.assertEqual(logged_in.status, 200, logged_in.body)
        return registered.json()["id"], logged_in.json()["access_token"]

    @staticmethod
    def configuration(instruments):
        def strategy(buy, sell):
            return {
                "strategy_type": "random",
                "buy_probability": buy,
                "sell_probability": sell,
                "hold_probability": 0,
                "price_deviation_pct": 1,
            }

        return {
            "name": "Regimex integration",
            "duration_steps": 32,
            "seed": 42,
            "background_initial_quote_balance": 1_000_000,
            "background_min_order_volume": 1,
            "background_max_order_volume": 1,
            "test_initial_quote_balance": 1_000_000,
            "test_min_order_volume": 1,
            "test_max_order_volume": 1,
            "instrument_configs": [
                {
                    "instrument_id": instrument,
                    "initial_price": price,
                    "background_initial_base_balance": 1000,
                    "test_initial_base_balance": 1000,
                }
                for instrument, price in zip(instruments, (100.005, 200), strict=True)
            ],
            "background_profiles": [
                {**strategy(100, 0), "agent_count": 1},
                {**strategy(0, 100), "agent_count": 1},
            ],
            "test_strategy": strategy(50, 50),
        }


if __name__ == "__main__":
    unittest.main()
