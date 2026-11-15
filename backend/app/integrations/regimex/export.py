"""Deterministic observations from a locked, completed simulation run.

Native step s maps to export bin [s - 1, s) in simulation-step units. The
export includes every bin, including inactivity. A final order book cannot
establish historical quotes, so both quote sides remain explicitly unknown.
"""

import hashlib
import json
import math
import re
from collections.abc import Collection
from decimal import Decimal, InvalidOperation, localcontext

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from backend.app.models import Order, SimulationRun, SimulationRunInstrument, Trade

MAX_STEPS = 4096
MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_COUNT = 2**31 - 1
QUANTUM = Decimal("0.00000001")
SOURCE_PATTERN = re.compile(r"[A-Za-z0-9._:@-]{1,128}\Z")


class ExportError(ValueError):
    """A safe explanation of why this run cannot be exported."""


def _number(value: Decimal, *, positive: bool = False) -> float:
    """Keep Decimal aggregation exact; refuse a lossy schema-2.1 conversion."""
    if not value.is_finite() or value < 0 or (positive and value == 0):
        raise ExportError("Run contains an invalid price or volume.")
    with localcontext() as context:
        context.prec = 60
        try:
            precise = value == value.quantize(QUANTUM)
        except InvalidOperation:
            precise = False
        if not precise:
            raise ExportError("Run values exceed the stored eight-decimal precision.")
    result = float(value)
    if not math.isfinite(result) or Decimal(str(result)) != value:
        raise ExportError("Run values cannot be exported without losing numeric precision.")
    return result


def _count(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise ExportError("Per-step activity exceeds the supported count limit.")
    return value


def _asset(value: object) -> tuple[int, str]:
    if not isinstance(value, dict):
        raise ExportError("Run lacks frozen asset metadata; start a new simulation run.")
    identity, symbol = value.get("id"), value.get("symbol")
    if (
        type(identity) is not int
        or identity <= 0
        or not isinstance(symbol, str)
        or not symbol.strip()
        or len(symbol) > 128
    ):
        raise ExportError("Run contains invalid frozen asset metadata.")
    return identity, symbol


def _instruments(db: Session, run: SimulationRun) -> dict[int, dict[str, object]]:
    snapshot = run.configuration_snapshot
    configs = snapshot.get("instrument_configs") if isinstance(snapshot, dict) else None
    if not isinstance(configs, list) or not 1 <= len(configs) <= 2:
        raise ExportError("Run requires frozen metadata for one or two instruments.")
    markets = list(
        db.scalars(
            select(SimulationRunInstrument)
            .where(
                SimulationRunInstrument.run_id == run.id,
            )
            .order_by(SimulationRunInstrument.instrument_id)
            .limit(3)
        )
    )
    if len(markets) != len(configs):
        raise ExportError("Frozen instrument metadata does not match the run.")
    prices = {market.instrument_id: market.initial_price for market in markets}
    instruments: dict[int, dict[str, object]] = {}
    quotes: set[tuple[int, str]] = set()
    identifiers: set[str] = set()
    for config in configs:
        if not isinstance(config, dict) or not isinstance(config.get("instrument"), dict):
            raise ExportError("Run lacks frozen instrument metadata; start a new simulation run.")
        identity = config.get("instrument_id")
        instrument = config["instrument"]
        if (
            type(identity) is not int
            or identity not in prices
            or identity in instruments
            or instrument.get("id") != identity
        ):
            raise ExportError("Frozen instrument metadata does not match the run.")
        base_id, base = _asset(instrument.get("base_asset"))
        quote_id, quote = _asset(instrument.get("quote_asset"))
        if (
            base_id == quote_id
            or base == quote
            or instrument.get("base_asset_id") != base_id
            or instrument.get("quote_asset_id") != quote_id
        ):
            raise ExportError("Frozen instrument assets are inconsistent.")
        identifier = f"{base}/{quote}"
        if len(identifier) > 128 or identifier in identifiers:
            raise ExportError("Frozen instrument names are ambiguous or too long.")
        quotes.add((quote_id, quote))
        identifiers.add(identifier)
        instruments[identity] = {
            "instrument_id": identifier,
            "base_asset": base,
            "quote_asset": quote,
            "initial_price": _number(prices[identity], positive=True),
            "units": {
                "price_unit": "quote_asset_per_base_asset",
                "volume_unit": "base_asset",
                "price_decimal_places": 8,
                "volume_decimal_places": 8,
            },
        }
    if len(quotes) != 1:
        raise ExportError("Run instruments must share the same quote asset.")
    return dict(sorted(instruments.items()))


def _validate_events(db: Session, run: SimulationRun, instruments: Collection[int]) -> None:
    for model in (Order, Trade):
        invalid = [
            model.step < 1,
            model.step > run.total_steps,
            model.instrument_id.not_in(instruments),
        ]
        if model is Order:
            invalid.append(Order.status.in_(("open", "partial")))
        if (
            db.scalar(
                select(model.id)
                .where(
                    model.run_id == run.id,
                    or_(*invalid),
                )
                .limit(1)
            )
            is not None
        ):
            raise ExportError("Run history is inconsistent with its completed time grid.")


def _trade_bins(db: Session, run_id: int) -> dict[tuple[int, int], tuple[int, float, float]]:
    grouped = (
        select(
            Trade.instrument_id.label("instrument_id"),
            Trade.step.label("step"),
            func.count(Trade.id).label("count"),
            func.sum(Trade.volume).label("volume"),
            func.max(Trade.sequence_number).label("last_sequence"),
        )
        .where(Trade.run_id == run_id)
        .group_by(Trade.instrument_id, Trade.step)
        .subquery()
    )
    statement = (
        select(
            grouped.c.instrument_id,
            grouped.c.step,
            grouped.c.count,
            grouped.c.volume,
            Trade.price,
        )
        .join(
            Trade,
            and_(
                Trade.run_id == run_id,
                Trade.sequence_number == grouped.c.last_sequence,
            ),
        )
        .limit(2 * MAX_STEPS + 1)
    )
    result = {}
    for instrument_id, step, count, volume, price in db.execute(statement):
        result[instrument_id, step] = (
            _count(count),
            _number(volume, positive=True),
            _number(price, positive=True),
        )
    if len(result) > 2 * MAX_STEPS:
        raise ExportError("Run trade history exceeds the supported time grid.")
    return result


def _order_bins(db: Session, run_id: int) -> dict[tuple[int, int, str], tuple[int, float]]:
    statement = (
        select(
            Order.instrument_id,
            Order.step,
            Order.side,
            func.count(Order.id),
            func.sum(Order.volume),
        )
        .where(
            Order.run_id == run_id,
            Order.status != "rejected",
        )
        .group_by(Order.instrument_id, Order.step, Order.side)
        .limit(4 * MAX_STEPS + 1)
    )
    result = {}
    for instrument_id, step, side, count, volume in db.execute(statement):
        result[instrument_id, step, side] = _count(count), _number(volume, positive=True)
    if len(result) > 4 * MAX_STEPS:
        raise ExportError("Run order history exceeds the supported time grid.")
    return result


def build_export(db: Session, run: SimulationRun, source_id: str) -> dict[str, object]:
    """Build an immutable export while the caller holds the owned run's shared lock.

    This function neither commits nor changes simulation state. PostgreSQL sums
    Numeric quantities exactly, returning at most one aggregate per step/side.
    Regimex source identities are stable across repeated exports and catalogue edits.
    """
    if not SOURCE_PATTERN.fullmatch(source_id):
        raise ExportError("A stable Regimex source namespace is required.")
    if run.status != "completed" or run.current_step != run.total_steps or not run.finished_at:
        raise ExportError("Analysis requires a fully completed simulation run.")
    if not 1 <= run.total_steps <= MAX_STEPS:
        raise ExportError("Regimex supports completed runs of at most 4096 steps.")
    if run.engine_version not in ("2", "3"):
        raise ExportError("This simulation engine version is not supported for export.")
    instruments = _instruments(db, run)
    _validate_events(db, run, instruments)
    trades, orders = _trade_bins(db, run.id), _order_bins(db, run.id)
    observations = []
    for identity, instrument in instruments.items():
        last_price = instrument["initial_price"]
        for native_step in range(1, run.total_steps + 1):
            execution = trades.get((identity, native_step))
            trade_count, trade_volume = (0, 0.0)
            if execution is not None:
                trade_count, trade_volume, last_price = execution
            buy_count, buy_volume = orders.get((identity, native_step, "BUY"), (0, 0.0))
            sell_count, sell_volume = orders.get((identity, native_step, "SELL"), (0, 0.0))
            observations.append(
                {
                    "instrument_id": instrument["instrument_id"],
                    "step": native_step - 1,
                    "last_price": last_price,
                    "trade_count": trade_count,
                    "trade_volume": trade_volume,
                    "buy_order_count": buy_count,
                    "buy_order_volume": buy_volume,
                    "sell_order_count": sell_count,
                    "sell_order_volume": sell_volume,
                    "best_bid": None,
                    "best_ask": None,
                    "price_state": "observed" if execution is not None else "carried",
                    "bid_state": "unknown",
                    "ask_state": "unknown",
                }
            )
    payload: dict[str, object] = {
        "schema_version": "2.1",
        "source": {
            "source_id": source_id,
            "source_version": f"engine-{run.engine_version}",
            "configuration_id": str(run.simulation_id),
            "run_id": str(run.id),
            "snapshot_id": f"run-{run.id}-observations-v1",
            "state_revision": f"completed-{run.total_steps}",
            "run_status": "completed",
            "consistency": "atomic_completed_snapshot",
        },
        "producer": {"name": "trade-regimex-export", "version": "1"},
        "time_grid": {
            "origin": 0.0,
            "unit": "simulation_step",
            "bin_width": 1.0,
            "completed_horizon": float(run.total_steps),
            "step_count": run.total_steps,
            "interval_bounds": "[start,stop)",
            "terminal_bin": "full_bins_only",
        },
        "capabilities": {
            "execution_prices": "complete",
            "trade_aggregates": "complete",
            "accepted_order_aggregates": "complete",
            "historical_quotes": {
                "mode": "unavailable",
                "coverage": "unavailable",
                "provenance": "The simulator does not retain historical order-book snapshots.",
                "complete_event_history_sha256": None,
                "reconstruction_policy": None,
            },
            "price_policy": "initial_then_carry",
            "trade_count_policy": "one_per_partial_execution",
            "order_count_policy": "accepted_submissions_including_immediate_fills",
            "rejected_order_policy": "excluded",
        },
        "instruments": list(instruments.values()),
        "observations": observations,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    payload["content_sha256"] = hashlib.sha256(canonical).hexdigest()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > MAX_EXPORT_BYTES:
        raise ExportError("Run export exceeds the 16 MiB size limit.")
    return payload
