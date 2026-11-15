"""Deterministic JSON and strict parsing at the external service boundary."""

import json
import math


def json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON key")
        result[name] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


def strict_json(data: bytes) -> object:
    return json.loads(
        data.decode("utf-8"),
        object_pairs_hook=_unique_pairs,
        parse_constant=_invalid_constant,
        parse_float=_finite_float,
    )
