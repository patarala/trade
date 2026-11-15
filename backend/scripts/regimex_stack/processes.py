"""Bounded commands and foreground supervision of owned process groups."""

import http.client
import json
import os
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path

from .files import save_json
from .state import process_identity


class Supervisor:
    def __init__(self, root):
        self.root = root
        self.children = []
        self.identities = {}

    def start(self, name, command, environment, cwd):
        log_path = self.root / "logs" / (name + ".log")
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as log:
            process = subprocess.Popen(
                [str(item) for item in command],
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.children.append((name, process))
        self.identities[process.pid] = process_identity(process.pid)
        return process

    def run(self, name, command, environment, cwd, *, timeout=180):
        process = self.start(name, command, environment, cwd)
        deadline = time.monotonic() + timeout
        while (info := self.exit_info(process)) is None:
            self.check(exclude=process)
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{name} timed out; inspect its private log.")
            time.sleep(0.1)
        if info.si_code != os.CLD_EXITED or info.si_status:
            raise RuntimeError(f"{name} failed; inspect its private log.")
        self.stop_group(name, process)
        self.children.remove((name, process))

    @staticmethod
    def exit_info(process):
        # Keep the leader unreaped until its entire group has stopped. Its PID
        # cannot be reused while descendants still need cleanup.
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)

    def check(self, *, exclude=None):
        for name, process in self.children:
            if process is not exclude and self.exit_info(process) is not None:
                raise RuntimeError(f"{name} exited; inspect its private log.")

    def ready(self, port, path, *, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check()
            try:
                status, _ = local_request(port, "GET", path)
                if status == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.2)
        raise RuntimeError("A local API did not become ready before its deadline.")

    def group_running(self, process):
        identity = self.identities[process.pid]
        if identity is None or process_identity(process.pid) != identity:
            return False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            except (OSError, IndexError):
                continue
            if fields[2] == str(process.pid) and fields[3] == str(process.pid) and fields[0] != "Z":
                return True
        return False

    def stop_group(self, name, process):
        if self.group_running(process):
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGINT if name == "postgres" else signal.SIGTERM)
            deadline = time.monotonic() + 15
            while self.group_running(process) and time.monotonic() < deadline:
                time.sleep(0.1)
            if self.group_running(process):
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        self.identities.pop(process.pid)

    def close(self):
        # Stop consumers before PostgreSQL, including model/simulation children.
        failed = []
        for name, process in reversed(self.children):
            try:
                self.stop_group(name, process)
            except (OSError, subprocess.TimeoutExpired):
                failed.append(name)
        self.children.clear()
        save_json(self.root / "status.json", {"state": "cleanup_failed" if failed else "stopped"})
        if failed:
            raise RuntimeError("Some owned process groups did not stop: " + ", ".join(failed))


def local_request(port, method, path, body=None, *, token=None):
    """No proxies, redirects, arbitrary destinations, or unbounded response reads."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    try:
        connection.request(
            method, path, None if body is None else json.dumps(body).encode(), headers
        )
        response = connection.getresponse()
        content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("Local bootstrap response exceeds its size limit.")
        return response.status, json.loads(content) if content else None
    finally:
        connection.close()
