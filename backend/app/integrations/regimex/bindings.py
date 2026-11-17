"""Small, short-lived export identities for polling immutable completed runs.

Observations are never cached. Ownership and remote snapshot metadata are checked
on every request. Downloads and submissions always rebuild the full local export.
"""

import copy
import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import SimulationRun, SimulationRunInstrument

from .export import EXPORTER_VERSION
from .json import json_bytes


class VerifiedExports:
    def __init__(
        self, *, capacity: int = 128, ttl: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self._entries: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires, identity = entry
            if self.clock() >= expires:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return copy.deepcopy(identity)

    def remember(self, key: str, export: dict) -> None:
        identity = copy.deepcopy({
            field: export[field]
            for field in ("source", "time_grid", "instruments", "content_sha256")
        })
        with self._lock:
            # Reading/polling must not extend the lifetime of an old digest.
            if key in self._entries and self.clock() < self._entries[key][0]:
                return
            self._entries[key] = (self.clock() + self.ttl, identity)
            self._entries.move_to_end(key)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)


def binding_key(
    db: Session, run: SimulationRun, *, subject: str, source_id: str, origin: str
) -> str:
    """Fingerprint the frozen inputs without scanning order/trade event tables.

    The simulator only writes events while the run is running, under its run lock.
    Completed runs have no supported edit operation. Direct SQL changes to their
    historical events require a backend restart; they are outside this contract.
    """
    markets = db.execute(
        select(SimulationRunInstrument.instrument_id, SimulationRunInstrument.initial_price)
        .where(SimulationRunInstrument.run_id == run.id)
        .order_by(SimulationRunInstrument.instrument_id)
        .limit(3)
    )
    value = {
        "exporter": EXPORTER_VERSION,
        "origin": origin,
        "subject": subject,
        "source_id": source_id,
        "run_id": run.id,
        "simulation_id": run.simulation_id,
        "engine": run.engine_version,
        "status": run.status,
        "step": run.current_step,
        "total": run.total_steps,
        "configuration": run.configuration_snapshot,
        "started_at": None if run.started_at is None else run.started_at.isoformat(),
        "finished_at": None if run.finished_at is None else run.finished_at.isoformat(),
        "instruments": [[identifier, str(price)] for identifier, price in markets],
    }
    return hashlib.sha256(json_bytes(value)).hexdigest()
