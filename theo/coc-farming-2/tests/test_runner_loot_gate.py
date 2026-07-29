"""FarmingRunner unit tests with a fake device."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from PIL import Image

from coc_farm2.adb import SafetyStatus
from coc_farm2.models import (
    AppBounds,
    ContactGroupAction,
    ContactSample,
    DeviceProfile,
    FarmConfig,
    LootReading,
    LootThresholds,
    Macro,
    OcrRegion,
    PixelProbe,
    Point,
    Rect,
    VariationConfig,
)
from coc_farm2.ocr import CallableOcrBackend
from coc_farm2.runner import CycleOutcome, FarmingRunner


def _profile() -> DeviceProfile:
    return DeviceProfile(
        serial="X",
        model="M",
        android_api=34,
        package="p",
        activity="a",
        app_version="1",
        logical_width=200,
        logical_height=100,
        raw_width=100,
        raw_height=200,
        rotation=1,
        touch_device="/dev/input/event0",
        app_bounds=AppBounds(0, 0, 200, 100),
    )


def _probe(name: str, r: int = 10, g: int = 20, b: int = 30) -> PixelProbe:
    return PixelProbe(
        name=name,
        x=5,
        y=5,
        radius=0,
        reference_rgb=(r, g, b),
        tolerance=0,
        required_matches=1,
        sample_count=1,
    )


def _group(x: int, y: int) -> ContactGroupAction:
    return ContactGroupAction(
        delay_ms=0,
        samples=(
            ContactSample(0, 0, x, y, "down"),
            ContactSample(20, 0, x, y, "up"),
        ),
    )


@dataclass
class FakeDevice:
    profile: DeviceProfile
    taps: list[tuple[int, int]] = field(default_factory=list)
    screenshots: int = 0
    full: bool = False
    home: bool = True
    match_ready: bool = True
    return_ready: bool = False
    steps: int = 0

    def safety_status(self, expected: DeviceProfile) -> SafetyStatus:
        return SafetyStatus(
            online=True,
            foreground=True,
            unlocked=True,
            logical_width=expected.logical_width,
            logical_height=expected.logical_height,
            rotation=expected.rotation,
            app_bounds=expected.app_bounds,
            app_version=expected.app_version,
        )

    def tap(self, x: int, y: int, duration_ms: int = 0) -> None:
        self.taps.append((x, y))
        self.steps += 1
        if self.steps >= 2:
            self.return_ready = True
            self.match_ready = False

    def inject_contacts(
        self, action: ContactGroupAction, profile: DeviceProfile
    ) -> None:
        self.inject_contact_session((action,), profile)

    def inject_contact_session(
        self,
        groups: Sequence[ContactGroupAction],
        profile: DeviceProfile,
    ) -> None:
        for group in groups:
            downs = [s for s in group.samples if s.phase == "down"]
            if downs:
                self.tap(downs[0].x, downs[0].y)

    def screenshot(self) -> Image.Image:
        self.screenshots += 1
        return Image.new("RGB", (200, 100), color=(0, 0, 0))


def _fast_runner_kwargs() -> dict[str, float]:
    return {
        "match_stable_s": 0.0,
        "loot_ocr_settle_s": 0.0,
        "loot_ocr_gap_s": 0.0,
        "next_post_tap_s": 0.0,
    }


def test_match_ready_uses_stable_next_button_when_loot_icon_changes() -> None:
    """The animated loot icon must not hide an otherwise ready opponent."""
    profile = _profile()
    device = FakeDevice(profile=profile)
    probes = {
        "home": _probe("home"),
        "gold-full": _probe("gold-full", 1, 1, 1),
        "elixir-full": _probe("elixir-full", 2, 2, 2),
        "match_ready_a": _probe("match_ready_a"),
        "match_ready_b": _probe("match_ready_b"),
        "return_ready_a": _probe("return_ready_a"),
        "return_ready_b": _probe("return_ready_b"),
    }
    clock = {"t": 0.0}

    def sleeper(seconds: float) -> None:
        clock["t"] += max(seconds, 0.01)

    def reader(probe: PixelProbe) -> bool:
        return probe.name == "match_ready_a"

    runner = FarmingRunner(
        device=device,
        profile=profile,
        config=FarmConfig(
            next_button=Point(150, 50),
            return_tap=Point(100, 80),
        ),
        probes=probes,
        ocr_regions={"gold": OcrRegion("gold", Rect(0, 0, 10, 10))},
        start_search=Macro(
            name="start_search",
            profile=profile,
            actions=(_group(20, 20),),
            approved=True,
        ),
        attack_templates=[
            Macro(
                name="attack-01",
                profile=profile,
                actions=(_group(40, 40),),
                approved=True,
            )
        ],
        ocr_backend=CallableOcrBackend(lambda _img: "1000"),
        probe_reader=reader,
        probe_group_reader=lambda group: all(reader(probe) for probe in group),
        sleeper=sleeper,
        monotonic=lambda: clock["t"],
        stop_event=threading.Event(),
        pause_event=threading.Event(),
        poll_interval_s=0.01,
        match_stable_s=0.0,
    )

    runner._wait_for_stable_match(timeout_s=0.1)


def test_cycle_retries_start_search_once_when_first_replay_stays_home() -> None:
    profile = _profile()
    device = FakeDevice(profile=profile)
    probes = {
        "home": _probe("home"),
        "gold-full": _probe("gold-full", 1, 1, 1),
        "elixir-full": _probe("elixir-full", 2, 2, 2),
        "match_ready_a": _probe("match_ready_a"),
        "match_ready_b": _probe("match_ready_b"),
        "return_ready_a": _probe("return_ready_a"),
        "return_ready_b": _probe("return_ready_b"),
    }
    home = True
    match = False
    ret = False
    starts = 0

    def reader(probe: PixelProbe) -> bool:
        if probe.name == "home":
            return home
        if probe.name.startswith("match_ready"):
            return match
        if probe.name.startswith("return_ready"):
            return ret
        return False

    original_tap = device.tap

    def tap(x: int, y: int, duration_ms: int = 0) -> None:
        nonlocal home, match, ret, starts
        original_tap(x, y, duration_ms)
        if (x, y) == (20, 20):
            starts += 1
            if starts == 2:
                home = False
                match = True
        elif (x, y) == (40, 40):
            match = False
            ret = True
        elif (x, y) == (100, 80):
            ret = False
            home = True

    device.tap = tap  # type: ignore[method-assign]
    clock = {"t": 0.0}

    def sleeper(seconds: float) -> None:
        clock["t"] += max(seconds, 0.01)

    runner = FarmingRunner(
        device=device,
        profile=profile,
        config=replace(
            FarmConfig(
                thresholds=LootThresholds(gold=100, elixir=100, dark=0),
                next_button=Point(150, 50),
                return_tap=Point(100, 80),
                match_ready_timeout_ms=100,
                return_timeout_ms=100,
                home_timeout_ms=100,
            ),
            variation=VariationConfig(coord_sigma_px=0, delay_sigma_ms=0),
        ),
        probes=probes,
        ocr_regions={
            "gold": OcrRegion("gold", Rect(0, 0, 10, 10)),
            "elixir": OcrRegion("elixir", Rect(10, 0, 20, 10)),
        },
        start_search=Macro(
            name="start_search",
            profile=profile,
            actions=(_group(20, 20),),
            approved=True,
        ),
        attack_templates=[
            Macro(
                name="attack-01",
                profile=profile,
                actions=(_group(40, 40),),
                approved=True,
            )
        ],
        ocr_backend=CallableOcrBackend(lambda _img: "500000"),
        probe_reader=reader,
        probe_group_reader=lambda group: all(reader(probe) for probe in group),
        sleeper=sleeper,
        monotonic=lambda: clock["t"],
        stop_event=threading.Event(),
        pause_event=threading.Event(),
        poll_interval_s=0.01,
        safety_interval_s=0,
        match_stable_s=0.0,
        loot_ocr_settle_s=0.0,
        loot_ocr_gap_s=0.0,
        next_post_tap_s=0.0,
    )

    assert runner.run_cycle() == CycleOutcome.COMPLETE
    assert starts == 2


def test_match_wait_dismisses_a_system_interruption_once() -> None:
    profile = _profile()
    device = FakeDevice(profile=profile)
    probes = {
        "home": _probe("home"),
        "gold-full": _probe("gold-full", 1, 1, 1),
        "elixir-full": _probe("elixir-full", 2, 2, 2),
        "match_ready_a": _probe("match_ready_a"),
        "match_ready_b": _probe("match_ready_b"),
        "return_ready_a": _probe("return_ready_a"),
        "return_ready_b": _probe("return_ready_b"),
    }
    match = False
    recoveries = 0
    clock = {"t": 0.0}
    logs: list[str] = []

    def reader(probe: PixelProbe) -> bool:
        return probe.name == "match_ready_a" and match

    def sleeper(seconds: float) -> None:
        clock["t"] += max(seconds, 0.01)

    def recover() -> str | None:
        nonlocal match, recoveries
        recoveries += 1
        match = True
        return "Mise à jour de l'opérateur"

    runner = FarmingRunner(
        device=device,
        profile=profile,
        config=FarmConfig(
            next_button=Point(150, 50),
            return_tap=Point(100, 80),
        ),
        probes=probes,
        ocr_regions={"gold": OcrRegion("gold", Rect(0, 0, 10, 10))},
        start_search=Macro(
            name="start_search",
            profile=profile,
            actions=(_group(20, 20),),
            approved=True,
        ),
        attack_templates=[
            Macro(
                name="attack-01",
                profile=profile,
                actions=(_group(40, 40),),
                approved=True,
            )
        ],
        ocr_backend=CallableOcrBackend(lambda _img: "1000"),
        probe_reader=reader,
        probe_group_reader=lambda group: all(reader(probe) for probe in group),
        sleeper=sleeper,
        monotonic=lambda: clock["t"],
        interruption_recoverer=recover,
        interruption_check_after_s=0.02,
        stop_event=threading.Event(),
        pause_event=threading.Event(),
        poll_interval_s=0.01,
        match_stable_s=0.0,
        on_log=logs.append,
    )

    runner._wait_for_stable_match(timeout_s=1.0)

    assert recoveries == 1
    assert any("dismissed system interruption" in message for message in logs)


def test_cycle_complete_with_good_loot() -> None:
    profile = _profile()
    device = FakeDevice(profile=profile)
    probes = {
        "home": _probe("home"),
        "gold-full": _probe("gold-full", 1, 1, 1),
        "elixir-full": _probe("elixir-full", 2, 2, 2),
        "match_ready_a": _probe("match_ready_a"),
        "match_ready_b": _probe("match_ready_b"),
        "return_ready_a": _probe("return_ready_a"),
        "return_ready_b": _probe("return_ready_b"),
    }
    state = {"home": True, "match": True, "ret": False, "full": False}

    def reader(probe: PixelProbe) -> bool:
        if probe.name == "home":
            return state["home"]
        if probe.name in {"gold-full", "elixir-full"}:
            return state["full"]
        if probe.name.startswith("match_ready"):
            return state["match"]
        if probe.name.startswith("return_ready"):
            return state["ret"]
        return False

    def group_reader(group: Sequence[PixelProbe]) -> bool:
        return all(reader(p) for p in group)

    def on_tap_side_effect() -> None:
        if len(device.taps) >= 2:
            state["ret"] = True
            state["match"] = False
            state["home"] = True

    original_tap = device.tap

    def tap(x: int, y: int, duration_ms: int = 0) -> None:
        original_tap(x, y, duration_ms)
        on_tap_side_effect()

    device.tap = tap  # type: ignore[method-assign]

    config = replace(
        FarmConfig(
            thresholds=LootThresholds(gold=100, elixir=100, dark=0),
            next_button=Point(150, 50),
            return_tap=Point(100, 80),
            match_ready_timeout_ms=5_000,
            return_timeout_ms=5_000,
            home_timeout_ms=5_000,
        ),
        variation=VariationConfig(coord_sigma_px=0, delay_sigma_ms=0),
    )
    start = Macro(
        name="start_search",
        profile=profile,
        actions=(_group(20, 20),),
        approved=True,
    )
    attack = Macro(
        name="attack-01",
        profile=profile,
        actions=(_group(40, 40),),
        approved=True,
    )
    regions = {
        "gold": OcrRegion("gold", Rect(0, 0, 10, 10)),
        "elixir": OcrRegion("elixir", Rect(10, 0, 20, 10)),
    }
    backend = CallableOcrBackend(lambda _img: "500000")

    runner = FarmingRunner(
        device=device,
        profile=profile,
        config=config,
        probes=probes,
        ocr_regions=regions,
        start_search=start,
        attack_templates=[attack],
        ocr_backend=backend,
        probe_reader=reader,
        probe_group_reader=group_reader,
        sleeper=lambda _s: None,
        stop_event=threading.Event(),
        pause_event=threading.Event(),
        poll_interval_s=0,
        safety_interval_s=0,
        **_fast_runner_kwargs(),
    )
    outcome = runner.run_cycle()
    assert outcome == CycleOutcome.COMPLETE
    assert (20, 20) in device.taps
    assert (40, 40) in device.taps
    assert (100, 80) in device.taps


def test_loot_gate_max_nexts_attacks_current() -> None:
    """If Next never changes the base, attack after max_nexts (coc_attack)."""
    profile = _profile()
    device = FakeDevice(profile=profile)
    probes = {
        "home": _probe("home"),
        "gold-full": _probe("gold-full", 1, 1, 1),
        "elixir-full": _probe("elixir-full", 2, 2, 2),
        "match_ready_a": _probe("match_ready_a"),
        "match_ready_b": _probe("match_ready_b"),
        "return_ready_a": _probe("return_ready_a"),
        "return_ready_b": _probe("return_ready_b"),
    }
    state = {"home": True, "match": True, "ret": False, "full": False}

    def reader(probe: PixelProbe) -> bool:
        if probe.name == "home":
            return state["home"]
        if probe.name in {"gold-full", "elixir-full"}:
            return state["full"]
        if probe.name.startswith("match_ready"):
            return state["match"]
        if probe.name.startswith("return_ready"):
            return state["ret"]
        return False

    def group_reader(group: Sequence[PixelProbe]) -> bool:
        return all(reader(p) for p in group)

    original_tap = device.tap

    def tap(x: int, y: int, duration_ms: int = 0) -> None:
        original_tap(x, y, duration_ms)
        if (x, y) == (40, 40):
            state["ret"] = True
            state["match"] = False
            state["home"] = True

    device.tap = tap  # type: ignore[method-assign]

    config = replace(
        FarmConfig(
            thresholds=LootThresholds(gold=1_000_000, elixir=1_000_000, dark=0),
            next_button=Point(150, 50),
            return_tap=Point(100, 80),
            max_nexts_per_cycle=3,
            match_ready_timeout_ms=1_000,
            return_timeout_ms=1_000,
            home_timeout_ms=1_000,
        ),
        variation=VariationConfig(coord_sigma_px=0, delay_sigma_ms=0),
    )
    start = Macro(
        name="start_search",
        profile=profile,
        actions=(_group(20, 20),),
        approved=True,
    )
    attack = Macro(
        name="attack-01",
        profile=profile,
        actions=(_group(40, 40),),
        approved=True,
    )
    regions = {
        "gold": OcrRegion("gold", Rect(0, 0, 10, 10)),
        "elixir": OcrRegion("elixir", Rect(10, 0, 20, 10)),
    }
    backend = CallableOcrBackend(lambda _img: "1000")
    logs: list[str] = []
    runner = FarmingRunner(
        device=device,
        profile=profile,
        config=config,
        probes=probes,
        ocr_regions=regions,
        start_search=start,
        attack_templates=[attack],
        ocr_backend=backend,
        probe_reader=reader,
        probe_group_reader=group_reader,
        sleeper=lambda _s: None,
        stop_event=threading.Event(),
        pause_event=threading.Event(),
        poll_interval_s=0,
        safety_interval_s=0,
        on_log=logs.append,
        **_fast_runner_kwargs(),
    )
    outcome = runner.run_cycle()
    assert outcome == CycleOutcome.COMPLETE
    assert device.taps.count((150, 50)) == 3  # exactly max_nexts, no retry storm
    assert (40, 40) in device.taps
    assert any("max Next presses" in msg for msg in logs)


def test_loot_gate_skips_low_loot_when_next_works() -> None:
    profile = _profile()
    device = FakeDevice(profile=profile)
    probes = {
        "home": _probe("home"),
        "gold-full": _probe("gold-full", 1, 1, 1),
        "elixir-full": _probe("elixir-full", 2, 2, 2),
        "match_ready_a": _probe("match_ready_a"),
        "match_ready_b": _probe("match_ready_b"),
        "return_ready_a": _probe("return_ready_a"),
        "return_ready_b": _probe("return_ready_b"),
    }
    state = {
        "home": True,
        "match": True,
        "ret": False,
        "full": False,
        "loot": "1000",
        "searching": False,
    }
    clock = {"t": 0.0}
    timestamps: dict[str, float] = {}

    def reader(probe: PixelProbe) -> bool:
        if probe.name == "home":
            return state["home"]
        if probe.name in {"gold-full", "elixir-full"}:
            return state["full"]
        if probe.name.startswith("match_ready"):
            return state["match"] and not state["searching"]
        if probe.name.startswith("return_ready"):
            return state["ret"]
        return False

    def group_reader(group: Sequence[PixelProbe]) -> bool:
        return all(reader(p) for p in group)

    original_tap = device.tap

    def tap(x: int, y: int, duration_ms: int = 0) -> None:
        original_tap(x, y, duration_ms)
        if (x, y) == (150, 50):
            timestamps["next"] = clock["t"]
            state["searching"] = True
            state["loot"] = "2000000"
        elif (x, y) == (40, 40):
            timestamps["attack"] = clock["t"]
            state["ret"] = True
            state["match"] = False
            state["home"] = True

    device.tap = tap  # type: ignore[method-assign]

    def sleeper(seconds: float) -> None:
        clock["t"] += max(seconds, 0.05)
        if state["searching"] and clock["t"] > 0.2:
            state["searching"] = False

    original_screenshot = device.screenshot

    def screenshot() -> Image.Image:
        clock["t"] += 1.0
        return original_screenshot()

    device.screenshot = screenshot  # type: ignore[method-assign]

    config = replace(
        FarmConfig(
            thresholds=LootThresholds(gold=1_000_000, elixir=1_000_000, dark=0),
            next_button=Point(150, 50),
            return_tap=Point(100, 80),
            match_ready_timeout_ms=5_000,
            return_timeout_ms=5_000,
            home_timeout_ms=5_000,
        ),
        variation=VariationConfig(coord_sigma_px=0, delay_sigma_ms=0),
    )
    start = Macro(
        name="start_search",
        profile=profile,
        actions=(_group(20, 20),),
        approved=True,
    )
    attack = Macro(
        name="attack-01",
        profile=profile,
        actions=(_group(40, 40),),
        approved=True,
    )
    regions = {
        "gold": OcrRegion("gold", Rect(0, 0, 10, 10)),
        "elixir": OcrRegion("elixir", Rect(10, 0, 20, 10)),
    }
    backend = CallableOcrBackend(lambda _img: state["loot"])
    runner = FarmingRunner(
        device=device,
        profile=profile,
        config=config,
        probes=probes,
        ocr_regions=regions,
        start_search=start,
        attack_templates=[attack],
        ocr_backend=backend,
        probe_reader=reader,
        probe_group_reader=group_reader,
        probe_frame_reader=lambda _image, selected: {
            name: reader(probe) for name, probe in selected.items()
        },
        sleeper=sleeper,
        monotonic=lambda: clock["t"],
        stop_event=threading.Event(),
        pause_event=threading.Event(),
        poll_interval_s=0.05,
        safety_interval_s=0,
        loot_ocr_settle_s=0,
        loot_ocr_gap_s=0,
    )
    outcome = runner.run_cycle()
    assert outcome == CycleOutcome.COMPLETE
    assert device.taps.count((150, 50)) == 1
    assert (40, 40) in device.taps
    assert device.screenshots == 2
    assert timestamps["attack"] - timestamps["next"] < 5.0


def test_loot_gate_reattacks_after_next_without_probe_drop() -> None:
    """Next succeeds with no match_ready drop; re-OCR decides (no retry taps)."""
    profile = _profile()
    device = FakeDevice(profile=profile)
    probes = {
        "home": _probe("home"),
        "gold-full": _probe("gold-full", 1, 1, 1),
        "elixir-full": _probe("elixir-full", 2, 2, 2),
        "match_ready_a": _probe("match_ready_a"),
        "match_ready_b": _probe("match_ready_b"),
        "return_ready_a": _probe("return_ready_a"),
        "return_ready_b": _probe("return_ready_b"),
    }
    state = {
        "home": True,
        "match": True,
        "ret": False,
        "full": False,
        "loot": "1000",
    }

    def reader(probe: PixelProbe) -> bool:
        if probe.name == "home":
            return state["home"]
        if probe.name in {"gold-full", "elixir-full"}:
            return state["full"]
        if probe.name.startswith("match_ready"):
            return state["match"]
        if probe.name.startswith("return_ready"):
            return state["ret"]
        return False

    def group_reader(group: Sequence[PixelProbe]) -> bool:
        return all(reader(p) for p in group)

    original_tap = device.tap

    def tap(x: int, y: int, duration_ms: int = 0) -> None:
        original_tap(x, y, duration_ms)
        if (x, y) == (150, 50):
            state["loot"] = "2000000"
        elif (x, y) == (40, 40):
            state["ret"] = True
            state["match"] = False
            state["home"] = True

    device.tap = tap  # type: ignore[method-assign]

    config = replace(
        FarmConfig(
            thresholds=LootThresholds(gold=1_000_000, elixir=1_000_000, dark=0),
            next_button=Point(150, 50),
            return_tap=Point(100, 80),
            match_ready_timeout_ms=5_000,
            return_timeout_ms=5_000,
            home_timeout_ms=5_000,
        ),
        variation=VariationConfig(coord_sigma_px=0, delay_sigma_ms=0),
    )
    runner = FarmingRunner(
        device=device,
        profile=profile,
        config=config,
        probes=probes,
        ocr_regions={
            "gold": OcrRegion("gold", Rect(0, 0, 10, 10)),
            "elixir": OcrRegion("elixir", Rect(10, 0, 20, 10)),
        },
        start_search=Macro(
            name="start_search",
            profile=profile,
            actions=(_group(20, 20),),
            approved=True,
        ),
        attack_templates=[
            Macro(
                name="attack-01",
                profile=profile,
                actions=(_group(40, 40),),
                approved=True,
            )
        ],
        ocr_backend=CallableOcrBackend(lambda _img: state["loot"]),
        probe_reader=reader,
        probe_group_reader=group_reader,
        sleeper=lambda _s: None,
        stop_event=threading.Event(),
        pause_event=threading.Event(),
        poll_interval_s=0,
        safety_interval_s=0,
        **_fast_runner_kwargs(),
    )
    outcome = runner.run_cycle()
    assert outcome == CycleOutcome.COMPLETE
    assert device.taps.count((150, 50)) == 1
    assert (40, 40) in device.taps


def test_loot_decision_skips_when_required_loot_is_uncertain() -> None:
    profile = _profile()
    device = FakeDevice(profile=profile)
    probes = {
        "home": _probe("home"),
        "gold-full": _probe("gold-full", 1, 1, 1),
        "elixir-full": _probe("elixir-full", 2, 2, 2),
        "match_ready_a": _probe("match_ready_a"),
        "match_ready_b": _probe("match_ready_b"),
        "return_ready_a": _probe("return_ready_a"),
        "return_ready_b": _probe("return_ready_b"),
    }
    config = replace(
        FarmConfig(
            thresholds=LootThresholds(gold=1_000_000, elixir=1_000_000, dark=0),
            loot_mode="sum",
            sum_threshold=2_000_000,
            next_button=Point(150, 50),
            return_tap=Point(100, 80),
        ),
        variation=VariationConfig(coord_sigma_px=0, delay_sigma_ms=0),
    )
    runner = FarmingRunner(
        device=device,
        profile=profile,
        config=config,
        probes=probes,
        ocr_regions={
            "gold": OcrRegion("gold", Rect(0, 0, 10, 10)),
            "elixir": OcrRegion("elixir", Rect(10, 0, 20, 10)),
        },
        start_search=Macro(
            name="start_search",
            profile=profile,
            actions=(_group(20, 20),),
            approved=True,
        ),
        attack_templates=[
            Macro(
                name="attack-01",
                profile=profile,
                actions=(_group(40, 40),),
                approved=True,
            )
        ],
        ocr_backend=CallableOcrBackend(lambda _img: "0"),
        probe_reader=lambda _p: False,
        probe_group_reader=lambda _g: False,
        stop_event=threading.Event(),
        pause_event=threading.Event(),
    )
    # A partial reading below the configured 2M sum remains unqualified.
    assert (
        runner._loot_decision(LootReading(gold=1_821_974, elixir=None, dark=None))
        == "next_partial"
    )
    # A readable lower bound over 2M qualifies even if the other field is missing.
    assert (
        runner._loot_decision(LootReading(gold=None, elixir=2_020_265, dark=None))
        == "attack"
    )
    # Clearly poor readable field → skip.
    assert (
        runner._loot_decision(LootReading(gold=1000, elixir=None, dark=None))
        == "next_partial"
    )
    # Totally blank is also unqualified.
    assert (
        runner._loot_decision(LootReading(gold=None, elixir=None, dark=None))
        == "next_partial"
    )


def test_cycle_finishes_battle_when_remaining_loot_reaches_ten_percent() -> None:
    profile = _profile()
    device = FakeDevice(profile=profile)
    probes = {
        "home": _probe("home"),
        "gold-full": _probe("gold-full", 1, 1, 1),
        "elixir-full": _probe("elixir-full", 2, 2, 2),
        "match_ready_a": _probe("match_ready_a"),
        "match_ready_b": _probe("match_ready_b"),
        "return_ready_a": _probe("return_ready_a"),
        "return_ready_b": _probe("return_ready_b"),
    }
    state = {
        "home": True,
        "match": True,
        "battle": False,
        "return": False,
    }
    loot = "1000"
    tap_screenshots: dict[tuple[int, int], int] = {}
    input_shell_release_screenshots: list[int] = []

    def reader(probe: PixelProbe) -> bool:
        if probe.name == "home":
            return state["home"]
        if probe.name.startswith("match_ready"):
            return state["match"]
        if probe.name.startswith("return_ready"):
            return state["return"]
        return False

    def frame_reader(
        _image: Image.Image, selected: Mapping[str, PixelProbe]
    ) -> dict[str, bool]:
        return {name: reader(probe) for name, probe in selected.items()}

    original_tap = device.tap

    def tap(x: int, y: int, duration_ms: int = 0) -> None:
        nonlocal loot
        original_tap(x, y, duration_ms)
        tap_screenshots[(x, y)] = device.screenshots
        if (x, y) == (40, 40):
            state.update(home=False, match=False, battle=True)
            loot = "100"
        elif (x, y) == (15, 80):
            state["battle"] = False
        elif (x, y) == (50, 80):
            state["battle"] = False
            state["return"] = True
        elif (x, y) == (100, 80):
            state["home"] = True
            state["return"] = False

    device.tap = tap  # type: ignore[method-assign]
    config = replace(
        FarmConfig(
            thresholds=LootThresholds(gold=100, elixir=100, dark=0),
            next_button=Point(150, 50),
            return_tap=Point(100, 80),
            finish_battle_tap=Point(15, 80),
            finish_battle_confirm_tap=Point(50, 80),
            finish_loot_ratio=0.1,
            match_ready_timeout_ms=1_000,
            return_timeout_ms=1_000,
            home_timeout_ms=1_000,
        ),
        variation=VariationConfig(coord_sigma_px=0, delay_sigma_ms=0),
    )
    runner = FarmingRunner(
        device=device,
        profile=profile,
        config=config,
        probes=probes,
        ocr_regions={
            "gold": OcrRegion("gold", Rect(0, 0, 10, 10)),
            "elixir": OcrRegion("elixir", Rect(10, 0, 20, 10)),
        },
        start_search=Macro(
            name="start_search",
            profile=profile,
            actions=(_group(20, 20),),
            approved=True,
        ),
        attack_templates=[
            Macro(
                name="attack-01",
                profile=profile,
                actions=(_group(40, 40),),
                approved=True,
            )
        ],
        ocr_backend=CallableOcrBackend(lambda _img: loot),
        probe_reader=reader,
        probe_group_reader=lambda group: all(reader(probe) for probe in group),
        probe_frame_reader=frame_reader,
        input_shell_releaser=lambda: input_shell_release_screenshots.append(
            device.screenshots
        ),
        sleeper=lambda _seconds: None,
        stop_event=threading.Event(),
        pause_event=threading.Event(),
        poll_interval_s=0,
        safety_interval_s=0,
        match_stable_s=0,
        loot_ocr_settle_s=0,
        loot_ocr_gap_s=0,
        next_post_tap_s=0,
    )

    assert runner.run_cycle() == CycleOutcome.COMPLETE
    assert device.taps.index((40, 40)) < device.taps.index((15, 80))
    assert device.taps.index((15, 80)) < device.taps.index((50, 80))
    assert device.taps.index((50, 80)) < device.taps.index((100, 80))
    assert tap_screenshots[(15, 80)] == tap_screenshots[(50, 80)]
    assert len(input_shell_release_screenshots) == 2


def test_cycle_dismisses_star_bonus_popup_before_waiting_for_home() -> None:
    profile = _profile()
    device = FakeDevice(profile=profile)
    probes = {
        "home": _probe("home"),
        "home-popup": _probe("home-popup"),
        "gold-full": _probe("gold-full", 1, 1, 1),
        "elixir-full": _probe("elixir-full", 2, 2, 2),
        "match_ready_a": _probe("match_ready_a"),
        "match_ready_b": _probe("match_ready_b"),
        "return_ready_a": _probe("return_ready_a"),
        "return_ready_b": _probe("return_ready_b"),
    }
    state = {
        "home": True,
        "match": True,
        "ret": False,
        "home_popup": False,
    }

    def reader(probe: PixelProbe) -> bool:
        if probe.name == "home":
            return state["home"]
        if probe.name == "home-popup":
            return state["home_popup"]
        if probe.name.startswith("match_ready"):
            return state["match"]
        if probe.name.startswith("return_ready"):
            return state["ret"]
        return False

    def frame_reader(
        _image: Image.Image, selected: Mapping[str, PixelProbe]
    ) -> dict[str, bool]:
        return {name: reader(probe) for name, probe in selected.items()}

    original_tap = device.tap

    def tap(x: int, y: int, duration_ms: int = 0) -> None:
        original_tap(x, y, duration_ms)
        if (x, y) == (40, 40):
            state.update(home=False, match=False, ret=True)
        elif (x, y) == (100, 80):
            state.update(ret=False, home_popup=True)
        elif (x, y) == (120, 90):
            state.update(home_popup=False, home=True)

    device.tap = tap  # type: ignore[method-assign]
    config = replace(
        FarmConfig(
            thresholds=LootThresholds(gold=100, elixir=100, dark=0),
            next_button=Point(150, 50),
            return_tap=Point(100, 80),
            home_popup_dismiss_tap=Point(120, 90),
            match_ready_timeout_ms=1_000,
            return_timeout_ms=1_000,
            home_timeout_ms=1_000,
        ),
        variation=VariationConfig(coord_sigma_px=0, delay_sigma_ms=0),
    )
    runner = FarmingRunner(
        device=device,
        profile=profile,
        config=config,
        probes=probes,
        ocr_regions={
            "gold": OcrRegion("gold", Rect(0, 0, 10, 10)),
            "elixir": OcrRegion("elixir", Rect(10, 0, 20, 10)),
        },
        start_search=Macro(
            name="start_search",
            profile=profile,
            actions=(_group(20, 20),),
            approved=True,
        ),
        attack_templates=[
            Macro(
                name="attack-01",
                profile=profile,
                actions=(_group(40, 40),),
                approved=True,
            )
        ],
        ocr_backend=CallableOcrBackend(lambda _img: "1000"),
        probe_reader=reader,
        probe_group_reader=lambda group: all(reader(probe) for probe in group),
        probe_frame_reader=frame_reader,
        sleeper=lambda _seconds: None,
        stop_event=threading.Event(),
        pause_event=threading.Event(),
        poll_interval_s=0,
        safety_interval_s=0,
        **_fast_runner_kwargs(),
    )

    assert runner.run_cycle() == CycleOutcome.COMPLETE
    assert (120, 90) in device.taps
