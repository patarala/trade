"""Read the saved analysis, result and optional HTML from one bounded response."""

import struct
from dataclasses import dataclass
from io import BytesIO
from zipfile import ZIP_STORED, BadZipFile, ZipFile

from .config import MAX_RESPONSE_BYTES
from .errors import invalid_response
from .json import strict_json

ZIP_OVERHEAD = 1024
_END_RECORD = struct.Struct("<4s4H2IH")
_ZIP64_LOCATOR_SIZE = 20


@dataclass(frozen=True, slots=True)
class Delivery:
    analysis: dict
    result: dict
    result_bytes: bytes
    report: bytes | None


def member_limits(response_bytes: int, include_report: bool) -> dict[str, int]:
    limit = min(response_bytes, MAX_RESPONSE_BYTES)
    limits = {"analysis.json": min(limit, 128 * 1024), "result.json": limit}
    if include_report:
        limits["report.html"] = limit
    return limits


def read_delivery(data: bytes, limits: dict[str, int]) -> Delivery:
    # Bound the central directory before ZipFile allocates its member index.
    # Regimex emits a single-disk ZIP with two or three entries and no comment.
    if not _END_RECORD.size <= len(data) <= sum(limits.values()) + ZIP_OVERHEAD:
        raise invalid_response()
    end = len(data) - _END_RECORD.size
    signature, disk, start_disk, count, total, size, offset, comment = _END_RECORD.unpack_from(
        data, end
    )
    if (
        signature != b"PK\x05\x06"
        or disk
        or start_disk
        or comment
        or count != len(limits)
        or total != count
        or size > ZIP_OVERHEAD
        or offset + size != end
        or data.startswith(b"PK\x06\x07", end - _ZIP64_LOCATOR_SIZE)
    ):
        raise invalid_response()
    try:
        with ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) != len(limits) or set(archive.namelist()) != set(limits):
                raise invalid_response()
            for info in infos:
                if (
                    info.orig_filename != info.filename
                    or info.compress_type != ZIP_STORED
                    or info.flag_bits & 1
                    or info.compress_size != info.file_size
                    or info.file_size > limits[info.filename]
                ):
                    raise invalid_response()
            analysis = strict_json(archive.read("analysis.json"))
            result_bytes = archive.read("result.json")
            result = strict_json(result_bytes)
            if not isinstance(analysis, dict) or not isinstance(result, dict):
                raise invalid_response()
            report = archive.read("report.html") if "report.html" in limits else None
            if report is not None and not report.decode("utf-8").strip():
                raise invalid_response()
            return Delivery(analysis, result, result_bytes, report)
    except (BadZipFile, ValueError, EOFError, OSError, RuntimeError):
        raise invalid_response() from None
