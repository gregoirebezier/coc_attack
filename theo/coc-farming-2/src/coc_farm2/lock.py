"""Single-runner process lock."""

from __future__ import annotations

import os
from pathlib import Path


class RunLockError(RuntimeError):
    """Another runner already holds the lock."""


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            existing = self.path.read_text(encoding="utf-8").strip()
            raise RunLockError(
                f"another runner holds {self.path} (pid {existing or '?'})"
            ) from error
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        try:
            self.path.unlink(missing_ok=True)
        finally:
            self._held = False

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()
