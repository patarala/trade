"""Bind every remote analysis to the owned immutable simulator export."""

import hashlib
import math
import re
from uuid import UUID

from .client import RegimexClient
from .errors import GatewayError, invalid_response
from .json import json_bytes

_STATES = {"queued", "running", "cancel_requested", "succeeded", "failed", "cancelled"}
_FAILURES = {
    "invalid_data": "Regimex could not analyze the exported observations.",
    "unsupported_configuration": "The selected model does not support this analysis configuration.",
    "incompatible_artifact": "The saved model or analysis artifact failed verification.",
    "capacity_limit": "The analysis exceeded the configured computation or storage limit.",
    "source_failure": "The analysis source was temporarily unavailable.",
    "infrastructure_failure": "Regimex encountered a temporary service failure.",
    "attempts_exhausted": "The analysis could not finish within its allowed attempts.",
}


def _require(condition: bool) -> None:
    if not condition:
        raise invalid_response()


def _uuid(value: object) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
        return value
    except ValueError, TypeError, AttributeError:
        raise invalid_response() from None


def _same_json(left: object, right: object) -> bool:
    try:
        return json_bytes(left) == json_bytes(right)
    except ValueError, TypeError, RecursionError:
        return False


def _snapshot(snapshot: dict, export: dict) -> str:
    identifier = _uuid(snapshot.get("snapshot_id"))
    source = export["source"]
    _require(
        all(
            snapshot.get(key) == value
            for key, value in {
                "source_id": source["source_id"],
                "run_id": source["run_id"],
                "source_snapshot_id": source["snapshot_id"],
                "content_sha256": export["content_sha256"],
            }.items()
        )
    )
    return identifier


def _analysis(value: dict, export: dict, expected: str | None = None) -> dict:
    identifier = _uuid(value.get("analysis_id"))
    _uuid(value.get("snapshot_id"))
    _require(expected is None or identifier == expected)
    source = export["source"]
    _require(
        all(
            value.get(key) == expected_value
            for key, expected_value in {
                "source_id": source["source_id"],
                "run_id": source["run_id"],
                "source_snapshot_id": source["snapshot_id"],
                "instrument_ids": [item["instrument_id"] for item in export["instruments"]],
                "step_count": export["time_grid"]["step_count"],
            }.items()
        )
    )
    _require(type(value.get("step_count")) is int)
    job = value.get("job")
    _require(isinstance(job, dict))
    _uuid(job.get("operation_id"))
    _require(job.get("kind") == "analysis" and job.get("resource_id") == identifier)
    state, stage = job.get("state"), job.get("stage")
    _require(isinstance(state, str) and state in _STATES)
    _require(isinstance(stage, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", stage) is not None)
    model = job.get("model")
    _require(isinstance(model, dict) and isinstance(model.get("model_version_id"), str))
    _require(1 <= len(model["model_version_id"]) <= 128)
    for field in ("bundle_sha256", "inference_policy_sha256"):
        _require(
            isinstance(model.get(field), str)
            and re.fullmatch(r"[a-f0-9]{64}", model[field]) is not None
        )
    for field in ("result_available", "report_available"):
        _require(type(value.get(field)) is bool)
        _require(not value[field] or state == "succeeded")
    _require(state != "succeeded" or value["result_available"])
    _require("failure" in job)
    failure = job["failure"]
    _require((failure is not None) == (state == "failed"))
    if failure is not None:
        _require(isinstance(failure, dict))
        code = failure.get("code")
        _require(isinstance(code, str) and code in _FAILURES)
        _require(type(failure.get("retryable")) is bool)
        _require(not failure["retryable"] or code in {"source_failure", "infrastructure_failure"})
    return value


def public_view(value: dict, run_id: int) -> dict:
    job = value["job"]
    failure = job["failure"]
    return {
        "analysis_id": value["analysis_id"],
        "run_id": run_id,
        "state": job["state"],
        "stage": job["stage"],
        "failure": None
        if failure is None
        else {
            "code": failure["code"],
            "message": _FAILURES[failure["code"]],
            "retryable": failure["retryable"],
        },
        "result_available": value["result_available"],
        "report_available": value["report_available"],
    }


class AnalysisGateway:
    def __init__(self, client: RegimexClient, export: dict) -> None:
        self.client = client
        self.export = export

    def bound(self, analysis_id: str) -> dict:
        identifier = _uuid(analysis_id)
        value = self.client.json("GET", f"/api/v1/integration/analyses/{identifier}")
        return self._bound_value(value, identifier)

    def _bound_value(self, value: dict, expected: str | None = None) -> dict:
        value = _analysis(value, self.export, expected)
        identifier = value["snapshot_id"]
        snapshot = self.client.json("GET", f"/api/v1/integration/snapshots/{identifier}")
        _require(_snapshot(snapshot, self.export) == identifier)
        return value

    def latest(self) -> dict | None:
        page = self.client.json("GET", "/api/v1/integration/analyses?limit=1")
        items = page.get("items")
        _require(isinstance(items, list) and len(items) <= 1)
        _require(type(page.get("limit")) is int and page["limit"] == 1)
        _require(type(page.get("offset")) is int and page["offset"] == 0)
        if not items:
            return None
        _require(isinstance(items[0], dict))
        return self._bound_value(items[0])

    def submit(self, retry_of: str | None = None) -> dict:
        if retry_of is not None:
            previous = self.bound(retry_of)
            if previous["job"]["state"] not in {"failed", "cancelled"}:
                raise GatewayError(
                    "retry_not_available",
                    "Only failed or cancelled analyses can be retried.",
                    409,
                )
        snapshot = self.client.json(
            "POST",
            "/api/v1/integration/snapshots",
            payload=self.export,
        )
        snapshot_id = _snapshot(snapshot, self.export)
        operation = {
            "operation": "trade-saved-model-analysis/1",
            "subject": self.client.subject,
            "source_id": self.export["source"]["source_id"],
            "run_id": self.export["source"]["run_id"],
            "snapshot_sha256": self.export["content_sha256"],
            "retry_of": retry_of,
        }
        key = "trade-" + hashlib.sha256(json_bytes(operation)).hexdigest()
        value = self.client.json(
            "POST",
            "/api/v1/integration/analyses",
            payload={"snapshot_id": snapshot_id, "model_version_id": "active"},
            idempotency_key=key,
        )
        _analysis(value, self.export)
        _require(value["snapshot_id"] == snapshot_id)
        return value

    def result(self, analysis_id: str, *, analysis: dict | None = None) -> dict:
        analysis = analysis if analysis is not None else self.bound(analysis_id)
        if not analysis["result_available"]:
            raise GatewayError("result_pending", "The analysis result is not ready.", 409)
        result = self.client.json("GET", f"/api/v1/integration/analyses/{analysis_id}/result")
        _require(
            all(
                _same_json(result.get(key), value)
                for key, value in {
                    "result_version": "2.0",
                    "analysis_id": analysis_id,
                    "snapshot_id": analysis["snapshot_id"],
                    "snapshot_sha256": self.export["content_sha256"],
                    "source": self.export["source"],
                    "time_grid": self.export["time_grid"],
                    "instruments": self.export["instruments"],
                    "model": analysis["job"]["model"],
                    "class_names": ["sideways", "growth", "decline"],
                }.items()
            )
        )
        self._result_grid(result)
        return result

    def _result_grid(self, result: dict) -> None:
        sequences = result.get("sequences")
        instruments = self.export["instruments"]
        count = self.export["time_grid"]["step_count"]
        _require(isinstance(sequences, list) and len(sequences) == len(instruments))
        for sequence, instrument in zip(sequences, instruments, strict=True):
            _require(
                isinstance(sequence, dict)
                and sequence.get("instrument_id") == instrument["instrument_id"]
            )
            probabilities, intervals = sequence.get("probabilities"), sequence.get("intervals")
            _require(isinstance(probabilities, list) and len(probabilities) == count)
            for row in probabilities:
                _require(isinstance(row, list) and len(row) == 3)
                _require(
                    all(
                        type(value) in {int, float} and 0 <= value <= 1 and math.isfinite(value)
                        for value in row
                    )
                )
                _require(abs(sum(row) - 1) <= 1e-6)
            _require(isinstance(intervals, list) and 1 <= len(intervals) <= count)
            previous = 0
            for interval in intervals:
                _require(
                    isinstance(interval, dict)
                    and interval.get("instrument_id") == instrument["instrument_id"]
                )
                start, stop = interval.get("start"), interval.get("stop")
                _require(
                    type(start) is int
                    and type(stop) is int
                    and start == previous
                    and start < stop <= count
                )
                status, direction = interval.get("status"), interval.get("direction")
                _require(status in ("observed", "no_trades", "insufficient_data"))
                _require(
                    direction in ("sideways", "growth", "decline")
                    if status == "observed"
                    else direction is None
                )
                previous = stop
            _require(previous == count)

    def report(self, analysis_id: str) -> bytes:
        analysis = self.bound(analysis_id)
        if not analysis["report_available"]:
            raise GatewayError("report_pending", "The analysis report is not ready.", 409)
        self.result(analysis_id, analysis=analysis)
        response = self.client.request("GET", f"/api/v1/integration/analyses/{analysis_id}/report")
        _require(response.content_type.split(";", 1)[0].strip().lower() == "text/html")
        try:
            _require(bool(response.body.decode("utf-8").strip()))
        except UnicodeError:
            raise invalid_response() from None
        return response.body
