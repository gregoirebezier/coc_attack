"""Operator alerts — terminal bell and optional logging."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class TerminalBellAlert:
    """Ring the terminal bell on an interval until acknowledgement."""

    write: Callable[[str], None] = lambda text: print(text, end="", file=sys.stdout)
    flush: Callable[[], None] = lambda: sys.stdout.flush()
    sleeper: Callable[[float], None] = time.sleep
    interval_s: float = 10.0

    def ring(self) -> None:
        self.write("\a")
        self.flush()

    def announce(self, message: str) -> None:
        self.write(f"\n*** {message} ***\n")
        self.write(
            "Press Enter after upgrades to recheck, or type q then Enter to stop.\n"
        )
        self.flush()
        self.ring()

    def wait_for_resume(
        self,
        *,
        input_fn: Callable[[str], str] = input,
        should_continue_alerting: Callable[[], bool] | None = None,
    ) -> bool:
        """
        Block until the operator presses Enter (resume) or types q (stop).

        Returns True to resume, False to stop.
        """
        self.ring()
        deadline = time.monotonic() + self.interval_s
        # Simple blocking input — bell rings once before prompt; operator
        # can re-trigger by waiting if we use a threaded approach later.
        # For reliability, ring, then block on input.
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining == 0:
                self.ring()
                deadline = time.monotonic() + self.interval_s
            try:
                # Non-select blocking: just input once after first ring.
                # Caller may re-enter if still full.
                response = input_fn("").strip().lower()
            except EOFError:
                return False
            if response in {"q", "quit", "stop"}:
                return False
            if response == "" or response in {"y", "yes", "resume", "ok"}:
                if should_continue_alerting is not None and should_continue_alerting():
                    self.announce("reserves still read full — upgrade more, then Enter")
                    continue
                return True
            self.write("Press Enter to resume, or q to stop.\n")
            self.flush()
