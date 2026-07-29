"""Farming state machine: search → loot gate → attack → return → reserves."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, assert_never

from PIL import Image

from coc_farm2.adb import SafetyStatus
from coc_farm2.loot import format_loot, meets_thresholds
from coc_farm2.models import (
    ContactGroupAction,
    DeviceProfile,
    FarmConfig,
    LootReading,
    Macro,
    MacroAction,
    OcrRegion,
    PixelProbe,
    Point,
    WaitAction,
    WaitPixelAction,
    WaitPixelsAction,
)
from coc_farm2.ocr import OcrBackend, read_loot
from coc_farm2.timing import apply_timing
from coc_farm2.variation import pick_attack_template, vary_macro


class RunnerFault(RuntimeError):
    """A fail-closed stop that prevents any further device input."""


class RunnerPaused(RunnerFault):
    """An explicit operator pause that cancels the active cycle."""


class CycleOutcome(Enum):
    COMPLETE = "complete"
    RESOURCES_FULL = "resources_full"


class RunnerDevice(Protocol):
    def safety_status(self, expected: DeviceProfile) -> SafetyStatus: ...

    def tap(self, x: int, y: int, duration_ms: int = 0) -> None: ...

    def inject_contacts(
        self, action: ContactGroupAction, profile: DeviceProfile
    ) -> None: ...

    def inject_contact_session(
        self,
        groups: Sequence[ContactGroupAction],
        profile: DeviceProfile,
    ) -> None: ...

    def screenshot(self) -> Image.Image: ...


def validate_safety_status(
    status: SafetyStatus,
    expected: DeviceProfile,
) -> None:
    if not status.online:
        raise RunnerFault("ADB device is offline or disconnected")
    if not status.foreground:
        raise RunnerFault("Clash of Clans is not in the foreground")
    if not status.unlocked:
        raise RunnerFault("device is locked")
    if (
        status.logical_width != expected.logical_width
        or status.logical_height != expected.logical_height
    ):
        raise RunnerFault("logical viewport changed")
    if status.rotation != expected.rotation:
        raise RunnerFault("display rotation changed")
    if status.app_bounds != expected.app_bounds:
        raise RunnerFault("app bounds changed")
    if status.app_version != expected.app_version:
        raise RunnerFault("Clash of Clans version changed")


@dataclass(slots=True)
class FarmingRunner:
    device: RunnerDevice
    profile: DeviceProfile
    config: FarmConfig
    probes: Mapping[str, PixelProbe]
    ocr_regions: Mapping[str, OcrRegion]
    start_search: Macro
    attack_templates: Sequence[Macro]
    ocr_backend: OcrBackend
    probe_reader: Callable[[PixelProbe], bool]
    probe_group_reader: Callable[[Sequence[PixelProbe]], bool]
    probe_cache_invalidator: Callable[[], None] = lambda: None
    input_shell_releaser: Callable[[], None] = lambda: None
    interruption_recoverer: Callable[[], str | None] = lambda: None
    # Optional fast path: evaluate many probes from one screenshot.
    probe_batch_reader: Callable[[Mapping[str, PixelProbe]], dict[str, bool]] | None = (
        None
    )
    # Evaluate scout probes against a screenshot already owned by the runner.
    probe_frame_reader: (
        Callable[[Image.Image, Mapping[str, PixelProbe]], dict[str, bool]] | None
    ) = None
    sleeper: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    stop_event: threading.Event | None = None
    pause_event: threading.Event | None = None
    # How often to re-run full dumpsys safety while injecting gestures.
    safety_interval_s: float = 5.0
    poll_interval_s: float = 0.1
    rng: random.Random | None = None
    on_log: Callable[[str], None] = lambda _msg: None
    # Loot-gate timing. Scout timer is ~30s — keep Next simple (coc_attack).
    match_stable_s: float = 0.0
    loot_ocr_settle_s: float = 0.8
    loot_ocr_gap_s: float = 0.12
    # After Suivant: brief settle then wait for match_ready (no confirm/retry taps).
    next_post_tap_s: float = 2.2
    interruption_check_after_s: float = 4.0
    _last_safety_at: float = 0.0

    def __post_init__(self) -> None:
        self.stop_event = self.stop_event or threading.Event()
        self.pause_event = self.pause_event or threading.Event()
        self.rng = self.rng or random.Random()
        self._last_safety_at = 0.0
        required = {
            "home",
            "gold-full",
            "elixir-full",
            "match_ready_a",
            "match_ready_b",
            "return_ready_a",
            "return_ready_b",
        }
        if self.config.home_popup_dismiss_tap is not None:
            required.add("home-popup")
        missing = sorted(required - set(self.probes))
        if missing:
            raise ValueError(f"missing required pixel probe(s): {', '.join(missing)}")
        if self.config.next_button is None:
            raise ValueError("config.next_button is not calibrated")
        if self.config.return_tap is None:
            raise ValueError("config.return_tap is not calibrated")
        if not self.attack_templates:
            raise ValueError("no attack templates provided")
        if "gold" not in self.ocr_regions and "elixir" not in self.ocr_regions:
            raise ValueError("at least one of gold/elixir OCR regions is required")

    def resources_full(self) -> bool:
        gold = self.probe_reader(self.probes["gold-full"])
        if gold:
            return True
        return self.probe_reader(self.probes["elixir-full"])

    def at_home(self) -> bool:
        return self.probe_reader(self.probes["home"])

    def run_cycle(self) -> CycleOutcome:
        self.on_log("preflight (safety + home + reserves)")
        self._check_safety(force=True)

        # One screenshot for home + both reserves when batch reader is available.
        if self.probe_batch_reader is not None:
            batch = self.probe_batch_reader(
                {
                    "home": self.probes["home"],
                    "gold-full": self.probes["gold-full"],
                    "elixir-full": self.probes["elixir-full"],
                }
            )
            if not batch.get("home"):
                raise RunnerFault("home checkpoint does not match at cycle start")
            if batch.get("gold-full") or batch.get("elixir-full"):
                return CycleOutcome.RESOURCES_FULL
        else:
            if not self.at_home():
                raise RunnerFault("home checkpoint does not match at cycle start")
            if self.resources_full():
                return CycleOutcome.RESOURCES_FULL

        self.on_log("playing start_search")
        self._start_search()

        self.on_log("entering loot gate")
        initial_loot = self._loot_gate()

        template = pick_attack_template(self.attack_templates, rng=self.rng)
        self.on_log(f"attack template {template.name!r}")
        self._play_macro(
            vary_macro(
                apply_timing(template, self.config.timing),
                self.config.variation,
                rng=self.rng,
            )
        )

        self.on_log("waiting for return screen")
        self._return_home(initial_loot)

        self._wait_for_home(timeout_s=self.config.home_timeout_ms / 1000)
        self._check_safety(force=True)
        if self.resources_full():
            return CycleOutcome.RESOURCES_FULL
        return CycleOutcome.COMPLETE

    def _match_ready_probes(self) -> tuple[PixelProbe, PixelProbe]:
        return (self.probes["match_ready_a"], self.probes["match_ready_b"])

    def _match_is_ready(self) -> bool:
        """Use the static Next button, not the animated loot icon, as readiness."""
        return self.probe_reader(self.probes["match_ready_a"])

    def _return_ready_probes(self) -> tuple[PixelProbe, PixelProbe]:
        return (self.probes["return_ready_a"], self.probes["return_ready_b"])

    def _start_search(self) -> None:
        """Replay search once more when a missed menu tap leaves us at home."""
        macro = apply_timing(
            self.start_search,
            self.config.timing,
            min_gap_ms=self.config.timing.start_search_gap_ms,
        )
        for attempt in range(2):
            self._play_macro(
                vary_macro(
                    macro,
                    self.config.variation,
                    rng=self.rng,
                )
            )
            self._sleep_interruptible(0.5)
            if self._match_is_ready():
                return
            if not self.at_home():
                return
            if attempt == 0:
                self.on_log("start_search stayed home — retrying once")
        raise RunnerFault("start_search stayed on home village after retry")

    def _loot_gate(self) -> LootReading:
        """Scout → OCR → attack or Next (coc_attack pick_village style).

        coc_attack does: OCR → tap Suivant once → sleep 4s → wait for battle UI.
        No "did Next work?" confirm loop and no retry taps — those were starting
        battles when Next had already succeeded (fault screens showed combat with
        above-threshold loot and no Suivant button).

        Missing fields are skipped unless a known sum already qualifies.
        Max skips still attacks current.
        """
        assert self.config.next_button is not None
        nexts = 0
        max_nexts = self.config.max_nexts_per_cycle
        while True:
            frame = self._wait_for_stable_match(
                timeout_s=self.config.match_ready_timeout_ms / 1000,
            )
            self._check_safety(force=False)
            reading = self._read_loot_settled(frame)
            self.on_log(f"loot {format_loot(reading)}")

            decision = self._loot_decision(reading)
            if decision == "attack":
                self.on_log("loot thresholds met")
                return reading
            if nexts >= max_nexts:
                self.on_log(f"max Next presses ({max_nexts}) — attacking current base")
                return reading

            reason = (
                "partial OCR — skipping"
                if decision == "next_partial"
                else "loot below threshold"
            )
            self.on_log(f"{reason} — tapping Next ({nexts + 1}/{max_nexts})")
            self._tap_next_and_settle()
            nexts += 1

    def _loot_decision(self, reading: LootReading) -> str:
        """Return attack | next | next_partial."""
        if self._loot_meets_thresholds(reading):
            return "attack"
        if self._loot_readable_for_thresholds(reading):
            return "next"
        # Clearly poor on a readable required field → skip.
        if self._any_required_below(reading):
            return "next_partial"
        # Missing fields that did not satisfy a sum lower bound remain unqualified.
        return "next_partial"

    def _loot_meets_thresholds(self, reading: LootReading) -> bool:
        return meets_thresholds(
            reading,
            self.config.thresholds,
            mode=self.config.loot_mode,
            sum_threshold=self.config.sum_threshold,
        )

    def _any_required_below(self, reading: LootReading) -> bool:
        if self.config.loot_mode == "sum":
            return False
        thresholds = self.config.thresholds
        if thresholds.gold > 0 and reading.gold is not None:
            if reading.gold < thresholds.gold:
                return True
        if thresholds.elixir > 0 and reading.elixir is not None:
            if reading.elixir < thresholds.elixir:
                return True
        if thresholds.dark > 0 and reading.dark is not None:
            if reading.dark < thresholds.dark:
                return True
        return False

    def _scout_frame_state(self, image: Image.Image) -> tuple[bool, bool]:
        """Return (match_ready, return_ready) from one shared screenshot."""
        selected = {
            "match_ready": self.probes["match_ready_a"],
            "return_ready_a": self.probes["return_ready_a"],
            "return_ready_b": self.probes["return_ready_b"],
        }
        if self.probe_frame_reader is not None:
            state = self.probe_frame_reader(image, selected)
            return (
                bool(state.get("match_ready")),
                bool(state.get("return_ready_a")) and bool(state.get("return_ready_b")),
            )
        # Compatibility path for custom/test readers that do not evaluate images.
        return (
            self._match_is_ready(),
            self.probe_group_reader(self._return_ready_probes()),
        )

    @staticmethod
    def _raise_if_return_screen(return_ready: bool) -> None:
        """Fail fast if the scout loop has entered the return/result UI."""
        if return_ready:
            raise RunnerFault(
                "left match UI during loot gate "
                "(return screen visible — end/return the battle manually)"
            )

    def _wait_for_stable_match(self, *, timeout_s: float) -> Image.Image:
        """Wait on fresh frames and return the frame that proves match readiness."""
        started_at = self.monotonic()
        deadline = started_at + timeout_s
        stable_since: float | None = None
        interruption_checked = False
        while self.monotonic() < deadline:
            self._check_controls()
            image = self.device.screenshot()
            match_ready, return_ready = self._scout_frame_state(image)
            self._raise_if_return_screen(return_ready)
            if match_ready:
                now = self.monotonic()
                if stable_since is None:
                    stable_since = now
                if now - stable_since >= self.match_stable_s:
                    return image
            else:
                stable_since = None
                now = self.monotonic()
                if (
                    not interruption_checked
                    and now - started_at >= self.interruption_check_after_s
                ):
                    interruption_checked = True
                    label = self.interruption_recoverer()
                    if label is not None:
                        self.on_log(f"dismissed system interruption: {label}")
                        self.probe_cache_invalidator()
                        self._sleep_interruptible(0.5)
            self._check_safety(force=False)
            self._sleep_interruptible(self.poll_interval_s)
        raise RunnerFault("timed out waiting for stable match_ready")

    def _tap_next_and_settle(self) -> None:
        """Single Suivant tap and a brief settle before the next fresh-frame wait.

        Mirrors coc_attack's single tap and settle. The loot-loop performs exactly
        one readiness wait before reading the next opponent.
        Deliberately no retry taps — a second tap after a successful Next can
        land on the map and start the battle (no Suivant button anymore).
        """
        assert self.config.next_button is not None
        point = self.config.next_button
        self._validate_point(point.x, point.y)
        self.device.tap(point.x, point.y)
        self.probe_cache_invalidator()
        if self.next_post_tap_s > 0:
            self._sleep_interruptible(self.next_post_tap_s)

    def _read_loot_settled(self, image: Image.Image) -> LootReading:
        """OCR the ready frame, retrying on fresh frames only for partial reads."""
        deadline = self.monotonic() + self.loot_ocr_settle_s
        last = LootReading(gold=None, elixir=None, dark=None)
        attempt = 0
        while True:
            self._check_controls()
            last = self._read_loot_once(image)
            attempt += 1
            if self._loot_meets_thresholds(last):
                return last
            if self._loot_readable_for_thresholds(last):
                if attempt > 1:
                    self.on_log(f"loot OCR settled on attempt {attempt}")
                return last
            # Partial but already enough to skip — don't burn more settle time.
            if self._any_required_below(last):
                return last
            if self.monotonic() >= deadline:
                return last
            self._sleep_interruptible(self.loot_ocr_gap_s)
            image = self._wait_for_stable_match(
                timeout_s=self.config.match_ready_timeout_ms / 1000,
            )

    def _read_loot_once(self, image: Image.Image) -> LootReading:
        return read_loot(image, self.ocr_regions, self.ocr_backend)

    def _loot_readable_for_thresholds(self, reading: LootReading) -> bool:
        if self.config.loot_mode == "sum":
            return reading.gold is not None and reading.elixir is not None
        thresholds = self.config.thresholds
        if thresholds.gold > 0 and reading.gold is None:
            return False
        if thresholds.elixir > 0 and reading.elixir is None:
            return False
        if thresholds.dark > 0 and reading.dark is None:
            return False
        # No positive thresholds: any readable resource is enough to decide.
        if thresholds.gold <= 0 and thresholds.elixir <= 0 and thresholds.dark <= 0:
            return reading.gold is not None or reading.elixir is not None
        return True

    def _return_home(self, initial_loot: LootReading) -> None:
        assert self.config.return_tap is not None
        # Keep the input shell down while polling consecutive screenshots.
        # Taps reopen it on demand; releasing it again avoids per-frame churn.
        self.input_shell_releaser()
        if (
            self.config.finish_battle_tap is None
            or self.config.finish_loot_ratio <= 0
        ):
            self._wait_for_probes(
                (self.probes["return_ready_a"], self.probes["return_ready_b"]),
                timeout_s=self.config.return_timeout_ms / 1000,
            )
            self._tap_return_home()
            return

        deadline = self.monotonic() + self.config.return_timeout_ms / 1000
        next_loot_check_at = self.monotonic()
        next_interruption_check_at = (
            self.monotonic() + self.interruption_check_after_s
        )
        finish_requested = False

        while True:
            self._check_controls()
            image = self.device.screenshot()
            _, return_ready = self._scout_frame_state(image)
            if return_ready:
                break

            now = self.monotonic()
            finish_point = self.config.finish_battle_tap
            if (
                not finish_requested
                and finish_point is not None
                and self.config.finish_loot_ratio > 0
                and now >= next_loot_check_at
            ):
                remaining = self._read_loot_once(image)
                ratio = self._remaining_loot_ratio(initial_loot, remaining)
                next_loot_check_at = (
                    now + self.config.finish_check_interval_ms / 1000
                )
                if ratio is not None:
                    self.on_log(f"battle loot remaining {ratio:.0%}")
                    if ratio <= self.config.finish_loot_ratio:
                        self._validate_point(finish_point.x, finish_point.y)
                        self.device.tap(finish_point.x, finish_point.y)
                        self.probe_cache_invalidator()
                        finish_requested = True
                        self.on_log("remaining loot threshold reached — ending battle")
                        confirm_point = self.config.finish_battle_confirm_tap
                        if confirm_point is not None:
                            self._sleep_interruptible(0.15)
                            self._validate_point(confirm_point.x, confirm_point.y)
                            self.device.tap(confirm_point.x, confirm_point.y)
                            self.probe_cache_invalidator()
                            self.on_log("confirmed early battle finish")
                        self.input_shell_releaser()

            if now >= next_interruption_check_at:
                self._recover_system_interruption()
                next_interruption_check_at = now + max(
                    self.interruption_check_after_s,
                    0.25,
                )

            if self.monotonic() >= deadline:
                raise RunnerFault("timed out waiting for return screen")
            self._check_safety(force=False)
            self._sleep_interruptible(self.poll_interval_s)

        self._tap_return_home()

    def _tap_return_home(self) -> None:
        assert self.config.return_tap is not None
        self._sleep_interruptible(0.05)
        point = self.config.return_tap
        self._validate_point(point.x, point.y)
        self.device.tap(point.x, point.y)
        self.probe_cache_invalidator()

    @staticmethod
    def _remaining_loot_ratio(
        initial: LootReading,
        remaining: LootReading,
    ) -> float | None:
        """Compare known gold + elixir only; partial OCR must never end a battle."""
        if (
            initial.gold is None
            or initial.elixir is None
            or remaining.gold is None
            or remaining.elixir is None
        ):
            return None
        initial_total = initial.gold + initial.elixir
        if initial_total <= 0:
            return None
        return max(0.0, (remaining.gold + remaining.elixir) / initial_total)

    def _play_macro(self, macro: Macro) -> None:
        if macro.profile != self.profile:
            raise RunnerFault("macro device profile does not match runner profile")
        # Batch consecutive contact groups into one on-device session so
        # inter-group gaps are slept inside a single JVM (not per-group
        # app_process startup, which made replay much slower than the take).
        batch: list[ContactGroupAction] = []

        def flush_batch() -> None:
            if not batch:
                return
            for group in batch:
                for sample in group.samples:
                    self._validate_point(sample.x, sample.y)
            self._check_safety(force=False)
            self.device.inject_contact_session(tuple(batch), self.profile)
            self.probe_cache_invalidator()
            batch.clear()

        for action in macro.actions:
            if isinstance(action, ContactGroupAction):
                batch.append(action)
                continue
            flush_batch()
            self._sleep_interruptible(action.delay_ms / 1000)
            self._check_safety(force=False)
            self._dispatch_non_contact(action)
        flush_batch()

    def _dispatch_non_contact(self, action: MacroAction) -> None:
        if isinstance(action, WaitAction):
            self._sleep_interruptible(action.duration_ms / 1000)
        elif isinstance(action, WaitPixelAction):
            probe = self.probes.get(action.probe_name)
            if probe is None:
                raise RunnerFault(
                    f"macro references missing probe {action.probe_name!r}"
                )
            self._wait_for_probe(probe, timeout_s=action.timeout_ms / 1000)
        elif isinstance(action, WaitPixelsAction):
            selected = tuple(self.probes[name] for name in action.probe_names)
            self._wait_for_probes(selected, timeout_s=action.timeout_ms / 1000)
        elif isinstance(action, ContactGroupAction):
            raise RunnerFault("contact groups must be flushed via session batching")
        else:
            assert_never(action)

    def _wait_for_probe(self, probe: PixelProbe, *, timeout_s: float) -> None:
        deadline = self.monotonic() + timeout_s
        while True:
            self._check_controls()
            if self.probe_reader(probe):
                return
            if self.monotonic() >= deadline:
                raise RunnerFault(
                    f"timed out waiting for pixel checkpoint {probe.name!r}"
                )
            self._check_safety(force=False)
            self._sleep_interruptible(self.poll_interval_s)

    def _wait_for_home(self, *, timeout_s: float) -> None:
        home_probe = self.probes["home"]
        popup_probe = self.probes.get("home-popup")
        popup_tap = self.config.home_popup_dismiss_tap
        deadline = self.monotonic() + timeout_s
        next_popup_tap_at = 0.0
        next_interruption_check_at = (
            self.monotonic() + self.interruption_check_after_s
        )

        while True:
            self._check_controls()
            if (
                popup_probe is not None
                and popup_tap is not None
                and self.probe_frame_reader is not None
            ):
                image = self.device.screenshot()
                state = self.probe_frame_reader(
                    image,
                    {"home": home_probe, "home-popup": popup_probe},
                )
                at_home = bool(state.get("home"))
                popup_visible = bool(state.get("home-popup"))
            else:
                at_home = self.probe_reader(home_probe)
                popup_visible = (
                    popup_probe is not None
                    and popup_tap is not None
                    and self.probe_reader(popup_probe)
                )

            if at_home:
                return

            now = self.monotonic()
            if popup_visible and popup_tap is not None and now >= next_popup_tap_at:
                self._validate_point(popup_tap.x, popup_tap.y)
                self.device.tap(popup_tap.x, popup_tap.y)
                self.probe_cache_invalidator()
                self.input_shell_releaser()
                self.on_log("dismissed home popup")
                next_popup_tap_at = now + 0.75

            if now >= next_interruption_check_at:
                self._recover_system_interruption()
                next_interruption_check_at = now + max(
                    self.interruption_check_after_s,
                    0.25,
                )

            if now >= deadline:
                raise RunnerFault(
                    f"timed out waiting for pixel checkpoint {home_probe.name!r}"
                )
            self._check_safety(force=False)
            self._sleep_interruptible(self.poll_interval_s)

    def _recover_system_interruption(self) -> bool:
        label = self.interruption_recoverer()
        if label is None:
            return False
        self.on_log(f"dismissed system interruption: {label}")
        self.probe_cache_invalidator()
        self._sleep_interruptible(0.5)
        return True

    def _wait_for_probes(
        self,
        probes: Sequence[PixelProbe],
        *,
        timeout_s: float,
    ) -> None:
        deadline = self.monotonic() + timeout_s
        while True:
            self._check_controls()
            if self.probe_group_reader(probes):
                return
            if self.monotonic() >= deadline:
                names = ", ".join(repr(probe.name) for probe in probes)
                raise RunnerFault(
                    f"timed out waiting for grouped pixel checkpoints {names}"
                )
            self._check_safety(force=False)
            self._sleep_interruptible(self.poll_interval_s)

    def _sleep_interruptible(self, duration_s: float) -> None:
        self._check_controls()
        if duration_s <= 0:
            self.sleeper(0)
            self._check_controls()
            return
        remaining = duration_s
        while remaining > 0:
            interval = min(remaining, 0.25)
            self.sleeper(interval)
            self._check_controls()
            remaining -= interval

    def _check_safety(self, *, force: bool = True) -> None:
        self._check_controls()
        now = self.monotonic()
        if (
            not force
            and self._last_safety_at > 0
            and (now - self._last_safety_at) < self.safety_interval_s
        ):
            return
        status = self.device.safety_status(self.profile)
        validate_safety_status(status, self.profile)
        self._last_safety_at = now

    def _check_controls(self) -> None:
        assert self.stop_event is not None
        assert self.pause_event is not None
        if self.stop_event.is_set():
            raise RunnerFault("runner stopped by operator")
        if self.pause_event.is_set():
            raise RunnerPaused("runner paused by operator")

    def _validate_point(self, x: int, y: int) -> None:
        if not self.profile.app_bounds.contains(x, y):
            raise RunnerFault(f"point ({x}, {y}) is outside the recorded app bounds")


def default_return_tap_from_probes(
    a: PixelProbe,
    b: PixelProbe,
) -> Point:
    """Midpoint between dual return-ready probes as a safe tap target."""
    return Point(x=(a.x + b.x) // 2, y=(a.y + b.y) // 2)
