"""Observation export against the simulator's real matching and step execution."""

import copy
import hashlib
import json
import unittest
from decimal import Decimal as D
from unittest.mock import patch

from sqlalchemy import select

from backend.app.integrations.regimex.export import ExportError, build_export
from backend.app.models import Asset, Order, SimulationRun, SimulationRunInstrument, Trade
from backend.app.schemas.market import InstrumentRead
from backend.app.services.simulation_execution import finish_run
from backend.app.services.trading import place_order
from backend.tests import test_simulation_execution as execution_fixture
from backend.tests import test_trading as trading_fixture


def freeze_instruments(db, run):
    """Use the same nested instrument schema saved by start_simulation."""
    snapshot = copy.deepcopy(run.configuration_snapshot)
    snapshot["instrument_configs"] = [
        {
            "instrument_id": market.instrument_id,
            "instrument": InstrumentRead.model_validate(market.instrument).model_dump(mode="json"),
            "initial_price": float(market.initial_price),
            "test_initial_base_balance": 10,
        }
        for market in db.scalars(
            select(SimulationRunInstrument)
            .where(
                SimulationRunInstrument.run_id == run.id,
            )
            .order_by(SimulationRunInstrument.instrument_id)
        )
    ]
    run.configuration_snapshot = snapshot
    db.commit()


class RegimexExportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = trading_fixture.TradingTests()
        self.fixture.setUp()
        self.db, self.run = self.fixture.db, self.fixture.run
        self.run.engine_version = "2"
        self.run.total_steps = 4
        freeze_instruments(self.db, self.run)

    def tearDown(self):
        self.fixture.tearDown()

    def order(self, participant, side, price, volume, *, step=2, instrument=1):
        return place_order(
            self.db,
            self.run.id,
            participant,
            instrument,
            side,
            D(price),
            D(volume),
            step,
        )

    def complete(self):
        self.run.current_step = self.run.total_steps
        self.db.flush()
        finish_run(self.db, self.run, "completed")
        self.db.commit()

    def export(self):
        return build_export(self.db, self.run, "trade-test")

    def test_partial_fills_accepted_orders_and_all_inactive_bins_are_preserved(self):
        self.order(2, "SELL", "105", "1")
        self.order(3, "SELL", "100", "1")
        buy = self.order(1, "BUY", "110", "1.5")
        rejected = self.order(1, "BUY", "100", "100")
        self.assertEqual(buy.status, "filled")
        self.assertEqual(rejected.status, "rejected")
        self.complete()
        self.fixture.assert_conserved()
        before_orders = [
            (x.id, x.status, x.reserved_amount) for x in self.db.scalars(select(Order))
        ]

        export = self.export()
        self.assertEqual(
            export["time_grid"],
            {
                "origin": 0.0,
                "unit": "simulation_step",
                "bin_width": 1.0,
                "completed_horizon": 4.0,
                "step_count": 4,
                "interval_bounds": "[start,stop)",
                "terminal_bin": "full_bins_only",
            },
        )
        rows = export["observations"]
        self.assertEqual(len(rows), 8)
        btc = [x for x in rows if x["instrument_id"] == "BTC/USD"]
        self.assertEqual([x["step"] for x in btc], [0, 1, 2, 3])
        self.assertEqual([x["last_price"] for x in btc], [100.0, 105.0, 105.0, 105.0])
        self.assertEqual(
            [x["price_state"] for x in btc], ["carried", "observed", "carried", "carried"]
        )
        self.assertEqual([x["trade_count"] for x in btc], [0, 2, 0, 0])
        self.assertEqual(btc[1]["trade_volume"], 1.5)
        self.assertEqual((btc[1]["buy_order_count"], btc[1]["buy_order_volume"]), (1, 1.5))
        self.assertEqual((btc[1]["sell_order_count"], btc[1]["sell_order_volume"]), (2, 2.0))
        self.assertTrue(all(x["trade_count"] == 0 for x in rows if x["instrument_id"] == "ETH/USD"))
        self.assertTrue(
            all(
                x["best_bid"] is None
                and x["best_ask"] is None
                and x["bid_state"] == "unknown"
                and x["ask_state"] == "unknown"
                for x in rows
            )
        )
        self.assertEqual(
            export["capabilities"]["historical_quotes"]["mode"],
            "unavailable",
        )
        self.assertEqual(
            before_orders,
            [(x.id, x.status, x.reserved_amount) for x in self.db.scalars(select(Order))],
        )
        self.fixture.assert_conserved()

    def test_digest_is_stable_after_catalog_edits_and_numeric_fields_are_floats(self):
        self.complete()
        original = self.export()
        usd = self.db.get(Asset, 1)
        usd.symbol, usd.name = "EUR", "Changed current catalogue"
        self.db.commit()
        self.assertEqual(self.export(), original)
        without_digest = {k: v for k, v in original.items() if k != "content_sha256"}
        canonical = json.dumps(
            without_digest,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        self.assertEqual(original["content_sha256"], hashlib.sha256(canonical).hexdigest())
        for row in original["observations"]:
            self.assertIs(type(row["last_price"]), float)
            for name in ("trade_volume", "buy_order_volume", "sell_order_volume"):
                self.assertIs(type(row[name]), float)
            self.assertIs(type(row["trade_count"]), int)
        self.assertNotIn("seed", original)
        self.assertNotIn("configuration_snapshot", original)

    def test_initial_price_comes_from_the_run_not_the_configuration(self):
        market = self.db.scalar(
            select(SimulationRunInstrument).where(
                SimulationRunInstrument.run_id == self.run.id,
                SimulationRunInstrument.instrument_id == 1,
            )
        )
        market.initial_price = D("100.01")
        self.complete()
        export = self.export()
        self.assertEqual(export["instruments"][0]["initial_price"], 100.01)
        self.assertEqual(export["observations"][0]["last_price"], 100.01)

    def test_unicode_frozen_asset_symbols_are_preserved(self):
        self.db.get(Asset, 2).symbol = "資産"
        freeze_instruments(self.db, self.run)
        self.complete()
        export = self.export()
        self.assertEqual(export["instruments"][0]["instrument_id"], "資産/USD")

    def test_incomplete_runs_unsupported_engines_and_long_runs_are_rejected(self):
        for status in ("running", "stopped", "failed"):
            with self.subTest(status=status):
                self.run.status = status
                with self.assertRaisesRegex(ExportError, "fully completed"):
                    self.export()
        self.run.status = "running"
        self.complete()
        self.run.current_step -= 1
        with self.assertRaisesRegex(ExportError, "fully completed"):
            self.export()
        self.run.current_step = self.run.total_steps
        self.run.engine_version = "1"
        with self.assertRaisesRegex(ExportError, "engine version"):
            self.export()
        self.run.engine_version = "3"
        self.run.current_step = self.run.total_steps = 4097
        with self.assertRaisesRegex(ExportError, "4096"):
            self.export()

    def test_missing_frozen_metadata_is_not_replaced_with_current_catalogue(self):
        self.complete()
        snapshot = copy.deepcopy(self.run.configuration_snapshot)
        del snapshot["instrument_configs"][0]["instrument"]
        self.run.configuration_snapshot = snapshot
        with self.assertRaisesRegex(ExportError, "frozen instrument metadata"):
            self.export()

    def test_out_of_grid_history_is_rejected_without_truncation(self):
        self.order(1, "BUY", "100", "1", step=0)
        self.complete()
        with self.assertRaisesRegex(ExportError, "time grid"):
            self.export()

    def test_unrepresentable_decimal_is_rejected_instead_of_rounding(self):
        self.complete()
        market = self.db.scalar(
            select(SimulationRunInstrument).where(
                SimulationRunInstrument.run_id == self.run.id,
                SimulationRunInstrument.instrument_id == 1,
            )
        )
        # Keep the exact Decimal in the identity map; SQLite cannot store this
        # precision, while the production Numeric(24,8) PostgreSQL column can.
        market.initial_price = D("1234567890123456.12345678")
        with self.assertRaisesRegex(ExportError, "losing numeric precision"):
            self.export()

    def test_count_and_encoded_size_limits_are_explicit(self):
        self.order(1, "BUY", "100", "1")
        self.complete()
        with (
            patch("backend.app.integrations.regimex.export.MAX_COUNT", 0),
            self.assertRaisesRegex(ExportError, "count limit"),
        ):
            self.export()
        with (
            patch("backend.app.integrations.regimex.export.MAX_EXPORT_BYTES", 100),
            self.assertRaisesRegex(ExportError, "16 MiB"),
        ):
            self.export()


class RegimexEngineExportTests(unittest.TestCase):
    def test_real_engine_completed_run_matches_all_saved_executions(self):
        fixture = execution_fixture.SimulationExecutionTests()
        fixture.setUp()
        try:
            run = fixture.db.get(SimulationRun, 1)
            freeze_instruments(fixture.db, run)
            fixture.run_all()
            run = fixture.db.get(SimulationRun, 1)
            export = build_export(fixture.db, run, "trade-engine-test")
            trades = list(fixture.db.scalars(select(Trade)))
            orders = list(fixture.db.scalars(select(Order)))
            self.assertGreater(len(trades), 0)
            for row in export["observations"]:
                instrument_id = 1 if row["instrument_id"] == "BTC/USD" else 2
                executed = [
                    x
                    for x in trades
                    if x.instrument_id == instrument_id and x.step == row["step"] + 1
                ]
                self.assertEqual(row["trade_count"], len(executed))
                self.assertEqual(
                    row["trade_volume"], float(sum((x.volume for x in executed), D(0)))
                )
                for side in ("BUY", "SELL"):
                    submitted = [
                        x
                        for x in orders
                        if x.instrument_id == instrument_id
                        and x.step == row["step"] + 1
                        and x.side == side
                        and x.status != "rejected"
                    ]
                    self.assertEqual(row[f"{side.lower()}_order_count"], len(submitted))
                    self.assertEqual(
                        row[f"{side.lower()}_order_volume"],
                        float(sum((x.volume for x in submitted), D(0))),
                    )
            fixture.fixture.assert_conserved()
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
