"""Private installation state and exclusive ownership of the local stack."""

import fcntl
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Stack directories cannot be symbolic links.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Stack directories must be private and owned by the current user.")


def read_private(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Stack state must be an owned private regular file.")
        value = stream.read(65537)
        if len(value) > 65536:
            raise ValueError("Stack state exceeds its size limit.")
        return value


def write_private(path: Path, value: str) -> None:
    if path.is_symlink():
        raise ValueError("Stack files cannot be symbolic links.")
    temporary = path.with_name(path.name + ".incoming-" + uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def save_json(path: Path, value: object) -> None:
    write_private(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


@contextmanager
def stack_lock(root: Path):
    fd = os.open(root / "stack.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("The stack lock must be an owned private regular file.")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another runner already owns this stack directory.") from None
        yield


def lock_is_held(root: Path) -> bool:
    """Inspect the existing lock without creating or changing stack state."""
    fd = os.open(root / "stack.lock", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
