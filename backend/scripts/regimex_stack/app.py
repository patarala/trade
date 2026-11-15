"""Serve the built simulator UI from the same local origin as its normal API."""

import os
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from backend.app.main import app

root = Path(os.environ["TRADE_STACK_FRONTEND"])
if not (root / "index.html").is_file():
    raise ValueError("The local stack requires a built frontend.")
app.mount("/", StaticFiles(directory=root, html=True), name="local-stack-ui")
