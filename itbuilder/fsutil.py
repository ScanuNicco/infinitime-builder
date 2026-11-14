"""Small file system helpers: atomic writes and inter-process locks."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write a file so that readers (and a crash) see either the old or the new content, never half."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class LockBusy(RuntimeError):
    """Raised by FileLock(blocking=False) when another process or thread holds the lock."""


class FileLock:
    """An exclusive lock on `path` (created if needed), held for the duration of a `with` block.

    Works across processes (e.g. the GUI and the CLI) and across threads, since every FileLock opens
    its own file. With blocking=False, raises LockBusy instead of waiting.
    """

    def __init__(self, path: Path, blocking: bool = True):
        self.path = Path(path)
        self.blocking = blocking
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
                except BlockingIOError as e:
                    raise LockBusy(str(self.path)) from e
            else:  # pragma: no cover - Windows
                while True:
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as e:
                        if not self.blocking:
                            raise LockBusy(str(self.path)) from e
                        time.sleep(0.2)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *exc) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
