"""Check the configured optional connection without database access or analysis jobs."""

import argparse
import json
from dataclasses import asdict

from pydantic import ValidationError

from backend.app.integrations.regimex.config import RegimexSettings
from backend.app.integrations.regimex.diagnostics import diagnose, invalid_configuration


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="Print credential-free JSON diagnostics."
    )
    arguments = parser.parse_args()
    try:
        settings = RegimexSettings()
    except ValidationError, OSError, UnicodeError:
        report = invalid_configuration()
    else:
        report = diagnose(settings)
    if arguments.json:
        print(json.dumps(asdict(report), sort_keys=True))
    else:
        labels = {"passed": "PASS", "failed": "FAIL", "not_checked": "SKIP", "disabled": "OFF"}
        for check in report.checks:
            print(f"{labels[check.status]} {check.name}: {check.message}")
    return int(report.status == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
