"""Local HTTP and owned temporary storage helpers for the optional integration test."""

import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body)


class Browser:
    """Request only the test's loopback HTTP server, without proxy environment use."""

    def __init__(self, origin: str):
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
            raise ValueError("Integration browser requires the owned loopback server.")
        self.port = parsed.port

    def request(self, method, path, *, token=None, payload=None, headers=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Use a local API path.")
        supplied = dict(headers or {})
        data = None
        if payload is not None:
            supplied["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        if token is not None:
            supplied["Authorization"] = "Bearer " + token
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(method, path, data, supplied)
            response = connection.getresponse()
            return Reply(
                response.status,
                {key.lower(): value for key, value in response.getheaders()},
                response.read(),
            )
        finally:
            connection.close()


@contextmanager
def tcp_server(factory):
    import uvicorn

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(
            factory(origin),
            host="127.0.0.1",
            log_level="warning",
            access_log=False,
            lifespan="on",
            timeout_graceful_shutdown=2,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started or not thread.is_alive():
            raise RuntimeError("The integration server did not start.")
        yield Browser(origin), origin
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("The integration server did not stop.")


@contextmanager
def private_workspace():
    root = Path(tempfile.mkdtemp(prefix="trade-regimex-integration-"))
    try:
        yield root
    finally:
        # Model caches may have read-only directories. Changing only owned
        # directory modes leaves any hardlinked model-file permissions intact.
        for current, directories, _files in os.walk(root, followlinks=False):
            Path(current).chmod(0o700)
            for name in directories:
                child = Path(current) / name
                if not child.is_symlink():
                    child.chmod(0o700)
        shutil.rmtree(root)
