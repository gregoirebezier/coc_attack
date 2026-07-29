"""Keyboard operator controls (pause / quit) for long runs."""

from __future__ import annotations

import select
import sys
import termios
import threading
import tty
from collections.abc import Callable


class OperatorControls:
    """Non-blocking stdin watcher for p/q during farming."""

    def __init__(
        self,
        *,
        stop_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        on_message: Callable[[str], None] | None = None,
    ) -> None:
        self.stop_event = stop_event or threading.Event()
        self.pause_event = pause_event or threading.Event()
        self.on_message = on_message or (lambda _msg: None)
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        if not sys.stdin.isatty():
            return
        self._running.set()
        self._thread = threading.Thread(
            target=self._loop,
            name="coc-farm2-controls",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _loop(self) -> None:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while self._running.is_set():
                ready, _, _ = select.select([sys.stdin], [], [], 0.25)
                if not ready:
                    continue
                char = sys.stdin.read(1)
                if char in {"q", "Q"}:
                    self.stop_event.set()
                    self.on_message("stop requested (q)")
                elif char in {"p", "P"}:
                    if self.pause_event.is_set():
                        self.pause_event.clear()
                        self.on_message("resumed")
                    else:
                        self.pause_event.set()
                        self.on_message("paused (p)")
        except (termios.error, OSError, ValueError):
            return
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except termios.error:
                pass
