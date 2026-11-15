"""Use the existing credential-free connection diagnostic with this stack's key."""

import json
import subprocess

from .files import read_private
from .state import service_environments


def doctor(root):
    state = json.loads(read_private(root / "stack.json"))
    credentials = json.loads(read_private(root / "credentials.json"))
    trade, _ = service_environments(root, state, credentials)
    trade["REGIMEX_TOKEN"] = read_private(root / "integration.token").strip()
    try:
        completed = subprocess.run(
            [root / "trade-venv" / "bin" / "python", "-m", "backend.scripts.regimex_doctor"],
            cwd=root,
            env=trade,
            stdin=subprocess.DEVNULL,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Connection diagnostics exceeded their deadline.") from None
    return completed.returncode
