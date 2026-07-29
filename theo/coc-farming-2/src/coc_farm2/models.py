"""Immutable domain models for device profile, macros, probes, and config."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class AppBounds:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if self.left < 0 or self.top < 0:
            raise ValueError("app bounds cannot start outside the display")
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("app bounds must have positive width and height")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def clamp(self, x: int, y: int) -> tuple[int, int]:
        return (
            min(self.right - 1, max(self.left, x)),
            min(self.bottom - 1, max(self.top, y)),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AppBounds:
        return cls(
            left=int(value["left"]),
            top=int(value["top"]),
            right=int(value["right"]),
            bottom=int(value["bottom"]),
        )


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    serial: str
    model: str
    android_api: int
    package: str
    activity: str
    app_version: str
    logical_width: int
    logical_height: int
    raw_width: int
    raw_height: int
    rotation: int
    touch_device: str
    app_bounds: AppBounds

    def __post_init__(self) -> None:
        if not self.serial:
            raise ValueError("device serial is required")
        if self.logical_width <= 0 or self.logical_height <= 0:
            raise ValueError("logical display dimensions must be positive")
        if self.raw_width <= 0 or self.raw_height <= 0:
            raise ValueError("raw touch dimensions must be positive")
        if self.rotation not in {0, 1, 2, 3}:
            raise ValueError("rotation must be one of 0, 1, 2, or 3")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DeviceProfile:
        return cls(
            serial=str(value["serial"]),
            model=str(value["model"]),
            android_api=int(value["android_api"]),
            package=str(value["package"]),
            activity=str(value["activity"]),
            app_version=str(value["app_version"]),
            logical_width=int(value["logical_width"]),
            logical_height=int(value["logical_height"]),
            raw_width=int(value["raw_width"]),
            raw_height=int(value["raw_height"]),
            rotation=int(value["rotation"]),
            touch_device=str(value["touch_device"]),
            app_bounds=AppBounds.from_dict(value["app_bounds"]),
        )


@dataclass(frozen=True, slots=True)
class ContactSample:
    """One absolute contact sample within a contact group (times from group start)."""

    t_ms: int
    finger_id: int
    x: int
    y: int
    phase: Literal["down", "move", "up"]

    def __post_init__(self) -> None:
        if self.t_ms < 0:
            raise ValueError("contact sample time cannot be negative")
        if self.finger_id < 0:
            raise ValueError("finger_id cannot be negative")
        if self.phase not in {"down", "move", "up"}:
            raise ValueError(f"unknown contact phase: {self.phase!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "t_ms": self.t_ms,
            "finger_id": self.finger_id,
            "x": self.x,
            "y": self.y,
            "phase": self.phase,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ContactSample:
        return cls(
            t_ms=int(value["t_ms"]),
            finger_id=int(value["finger_id"]),
            x=int(value["x"]),
            y=int(value["y"]),
            phase=str(value["phase"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ContactGroupAction:
    """
    One contiguous contact bout (1–10 fingers) for exact MotionEvent replay.

    Samples are timed from the first finger-down in the group. CoC interprets
    holds, drags, and chords — we do not classify them. Two-handed deploys are
    first-class (not trimmed as ghosts).
    """

    delay_ms: int
    samples: tuple[ContactSample, ...]
    kind: Literal["contacts"] = "contacts"

    def __post_init__(self) -> None:
        _validate_delay(self.delay_ms)
        if not self.samples:
            raise ValueError("contact group requires at least one sample")
        finger_ids = {sample.finger_id for sample in self.samples}
        if len(finger_ids) > 10:
            raise ValueError("contact group supports at most 10 fingers")
        if min(finger_ids) < 0:
            raise ValueError("finger_id cannot be negative")
        if max(finger_ids) >= 10:
            raise ValueError("finger_id must be in 0..9")
        times = [sample.t_ms for sample in self.samples]
        if any(t < 0 for t in times):
            raise ValueError("contact sample times cannot be negative")

    @property
    def duration_ms(self) -> int:
        return max(sample.t_ms for sample in self.samples)

    @property
    def finger_count(self) -> int:
        return len({sample.finger_id for sample in self.samples})

    def finger_paths(self) -> tuple[tuple[tuple[int, int, int], ...], ...]:
        """Return per-finger (x, y, t_ms) paths sorted by finger_id for inject."""
        by_finger: dict[int, list[tuple[int, int, int]]] = {}
        for sample in self.samples:
            by_finger.setdefault(sample.finger_id, []).append(
                (sample.x, sample.y, sample.t_ms)
            )
        paths: list[tuple[tuple[int, int, int], ...]] = []
        for finger_id in sorted(by_finger):
            points = by_finger[finger_id]
            points.sort(key=lambda item: item[2])
            # Injector needs a real hold interval for single-sample taps.
            if len(points) == 1:
                x, y, t0 = points[0]
                points = [(x, y, t0), (x, y, max(t0, 1))]
            paths.append(tuple(points))
        return tuple(paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "delay_ms": self.delay_ms,
            "samples": [sample.to_dict() for sample in self.samples],
        }


@dataclass(frozen=True, slots=True)
class WaitAction:
    delay_ms: int
    duration_ms: int
    kind: Literal["wait"] = "wait"

    def __post_init__(self) -> None:
        _validate_delay(self.delay_ms)
        if self.duration_ms < 0:
            raise ValueError("wait duration cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "delay_ms": self.delay_ms,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class WaitPixelAction:
    delay_ms: int
    probe_name: str
    timeout_ms: int
    kind: Literal["wait_pixel"] = "wait_pixel"

    def __post_init__(self) -> None:
        _validate_delay(self.delay_ms)
        if not self.probe_name:
            raise ValueError("pixel wait requires a probe name")
        if self.timeout_ms <= 0:
            raise ValueError("pixel wait timeout must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "delay_ms": self.delay_ms,
            "probe_name": self.probe_name,
            "timeout_ms": self.timeout_ms,
        }


@dataclass(frozen=True, slots=True)
class WaitPixelsAction:
    delay_ms: int
    probe_names: tuple[str, ...]
    timeout_ms: int
    kind: Literal["wait_pixels"] = "wait_pixels"

    def __post_init__(self) -> None:
        _validate_delay(self.delay_ms)
        if len(self.probe_names) < 2:
            raise ValueError("multi-pixel wait requires at least two probe names")
        if any(not name for name in self.probe_names):
            raise ValueError("multi-pixel wait probe names cannot be empty")
        if len(set(self.probe_names)) != len(self.probe_names):
            raise ValueError("multi-pixel wait probe names must be unique")
        if self.timeout_ms <= 0:
            raise ValueError("multi-pixel wait timeout must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "delay_ms": self.delay_ms,
            "probe_names": list(self.probe_names),
            "timeout_ms": self.timeout_ms,
        }


type MacroAction = ContactGroupAction | WaitAction | WaitPixelAction | WaitPixelsAction


def action_from_dict(value: dict[str, Any]) -> MacroAction:
    kind = value.get("kind")
    if kind == "contacts":
        raw_samples = value.get("samples") or ()
        if not isinstance(raw_samples, list):
            raise ValueError("contact group samples must be a list")
        samples = tuple(
            ContactSample.from_dict(item)
            for item in raw_samples
            if isinstance(item, dict)
        )
        return ContactGroupAction(
            delay_ms=int(value["delay_ms"]),
            samples=samples,
        )
    if kind == "wait":
        return WaitAction(
            delay_ms=int(value["delay_ms"]),
            duration_ms=int(value["duration_ms"]),
        )
    if kind == "wait_pixel":
        return WaitPixelAction(
            delay_ms=int(value["delay_ms"]),
            probe_name=str(value["probe_name"]),
            timeout_ms=int(value["timeout_ms"]),
        )
    if kind == "wait_pixels":
        probe_names = value["probe_names"]
        if not isinstance(probe_names, list):
            raise ValueError("multi-pixel wait probe_names must be a list")
        return WaitPixelsAction(
            delay_ms=int(value["delay_ms"]),
            probe_names=tuple(str(name) for name in probe_names),
            timeout_ms=int(value["timeout_ms"]),
        )
    raise ValueError(
        f"unknown macro action kind: {kind!r} "
        "(legacy tap/swipe/pinch/multi macros need recompile from getevent)"
    )


@dataclass(frozen=True, slots=True)
class RecordedTake:
    name: str
    profile: DeviceProfile
    actions: tuple[MacroAction, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "profile": self.profile.to_dict(),
            "actions": [action.to_dict() for action in self.actions],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RecordedTake:
        _validate_schema(value)
        return cls(
            name=str(value["name"]),
            profile=DeviceProfile.from_dict(value["profile"]),
            actions=tuple(action_from_dict(action) for action in value["actions"]),
        )


@dataclass(frozen=True, slots=True)
class Macro:
    name: str
    profile: DeviceProfile
    actions: tuple[MacroAction, ...]
    approved: bool = False
    source_take_name: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("macro name is required")
        if not self.actions:
            raise ValueError("macro must contain at least one action")
        if self.source_take_name == "":
            raise ValueError("source take name cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "profile": self.profile.to_dict(),
            "actions": [action.to_dict() for action in self.actions],
            "approved": self.approved,
            "source_take_name": self.source_take_name,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Macro:
        _validate_schema(value)
        return cls(
            name=str(value["name"]),
            profile=DeviceProfile.from_dict(value["profile"]),
            actions=tuple(action_from_dict(action) for action in value["actions"]),
            approved=bool(value.get("approved", False)),
            source_take_name=(
                str(value["source_take_name"])
                if value.get("source_take_name") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class PixelProbe:
    name: str
    x: int
    y: int
    radius: int
    reference_rgb: tuple[int, int, int]
    tolerance: int
    required_matches: int = 2
    sample_count: int = 3

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("probe name is required")
        if self.x < 0 or self.y < 0:
            raise ValueError("probe coordinates cannot be negative")
        if self.radius < 0:
            raise ValueError("probe radius cannot be negative")
        if len(self.reference_rgb) != 3 or any(
            channel < 0 or channel > 255 for channel in self.reference_rgb
        ):
            raise ValueError("reference_rgb must contain three byte values")
        if self.tolerance < 0:
            raise ValueError("probe tolerance cannot be negative")
        if self.sample_count <= 0:
            raise ValueError("sample_count must be positive")
        if not 1 <= self.required_matches <= self.sample_count:
            raise ValueError("required_matches must be between one and sample_count")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "radius": self.radius,
            "reference_rgb": list(self.reference_rgb),
            "tolerance": self.tolerance,
            "required_matches": self.required_matches,
            "sample_count": self.sample_count,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PixelProbe:
        rgb = value["reference_rgb"]
        return cls(
            name=str(value["name"]),
            x=int(value["x"]),
            y=int(value["y"]),
            radius=int(value["radius"]),
            reference_rgb=(int(rgb[0]), int(rgb[1]), int(rgb[2])),
            tolerance=int(value["tolerance"]),
            required_matches=int(value.get("required_matches", 2)),
            sample_count=int(value.get("sample_count", 3)),
        )


@dataclass(frozen=True, slots=True)
class Point:
    x: int
    y: int

    def to_dict(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Point:
        return cls(x=int(value["x"]), y=int(value["y"]))


@dataclass(frozen=True, slots=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("rect must have positive width and height")
        if self.left < 0 or self.top < 0:
            raise ValueError("rect cannot start outside the display")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Rect:
        return cls(
            left=int(value["left"]),
            top=int(value["top"]),
            right=int(value["right"]),
            bottom=int(value["bottom"]),
        )


@dataclass(frozen=True, slots=True)
class OcrRegion:
    name: str
    rect: Rect
    scale: float = 2.0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("OCR region name is required")
        if self.scale <= 0:
            raise ValueError("OCR scale must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rect": self.rect.to_dict(),
            "scale": self.scale,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OcrRegion:
        return cls(
            name=str(value["name"]),
            rect=Rect.from_dict(value["rect"]),
            scale=float(value.get("scale", 2.0)),
        )


@dataclass(frozen=True, slots=True)
class LootThresholds:
    gold: int = 400_000
    elixir: int = 400_000
    dark: int = 0

    def __post_init__(self) -> None:
        if min(self.gold, self.elixir, self.dark) < 0:
            raise ValueError("loot thresholds cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {"gold": self.gold, "elixir": self.elixir, "dark": self.dark}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LootThresholds:
        return cls(
            gold=int(value.get("gold", 400_000)),
            elixir=int(value.get("elixir", 400_000)),
            dark=int(value.get("dark", 0)),
        )


@dataclass(frozen=True, slots=True)
class VariationConfig:
    coord_sigma_px: float = 4.0
    # Keep small — large sigma re-adds gaps after delay_scale=0.
    delay_sigma_ms: float = 8.0

    def __post_init__(self) -> None:
        if self.coord_sigma_px < 0 or self.delay_sigma_ms < 0:
            raise ValueError("variation sigmas cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "coord_sigma_px": self.coord_sigma_px,
            "delay_sigma_ms": self.delay_sigma_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> VariationConfig:
        return cls(
            coord_sigma_px=float(value.get("coord_sigma_px", 4.0)),
            delay_sigma_ms=float(value.get("delay_sigma_ms", 8.0)),
        )


@dataclass(frozen=True, slots=True)
class TimingConfig:
    """Replay pacing for contact timelines (gaps only — never reshape holds)."""

    # Scale inter-group gaps only (finger-up → next bout). 1.0 = recorded.
    # Intra-group sample times are never scaled.
    delay_scale: float = 1.0
    # Floor for scaled inter-group gaps.
    min_delay_ms: int = 0
    # Min gap between start_search groups (Attack menu animations).
    start_search_gap_ms: int = 500

    def __post_init__(self) -> None:
        if self.delay_scale < 0:
            raise ValueError("delay_scale must be >= 0")
        if self.min_delay_ms < 0:
            raise ValueError("min_delay_ms cannot be negative")
        if self.start_search_gap_ms < 0:
            raise ValueError("start_search_gap_ms cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "delay_scale": self.delay_scale,
            "min_delay_ms": self.min_delay_ms,
            "start_search_gap_ms": self.start_search_gap_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TimingConfig:
        # Ignore obsolete farming-compile keys from older configs.
        return cls(
            delay_scale=float(value.get("delay_scale", 1.0)),
            min_delay_ms=int(value.get("min_delay_ms", 0)),
            start_search_gap_ms=int(value.get("start_search_gap_ms", 500)),
        )


@dataclass(frozen=True, slots=True)
class FarmConfig:
    thresholds: LootThresholds = LootThresholds()
    loot_mode: Literal["all", "sum"] = "all"
    sum_threshold: int = 0
    variation: VariationConfig = VariationConfig()
    timing: TimingConfig = TimingConfig()
    next_button: Point | None = None
    return_tap: Point | None = None
    home_popup_dismiss_tap: Point | None = None
    finish_battle_tap: Point | None = None
    finish_battle_confirm_tap: Point | None = None
    finish_loot_ratio: float = 0.0
    finish_check_interval_ms: int = 2_000
    max_nexts_per_cycle: int = 50
    match_ready_timeout_ms: int = 60_000
    return_timeout_ms: int = 180_000
    home_timeout_ms: int = 180_000
    long_gesture_threshold_ms: int = 3_000

    def __post_init__(self) -> None:
        if self.loot_mode not in {"all", "sum"}:
            raise ValueError(f"unsupported loot mode: {self.loot_mode!r}")
        if self.sum_threshold < 0:
            raise ValueError("sum_threshold cannot be negative")
        if self.loot_mode == "sum" and self.sum_threshold <= 0:
            raise ValueError("sum_threshold must be positive in sum mode")
        if not 0 <= self.finish_loot_ratio <= 1:
            raise ValueError("finish_loot_ratio must be between zero and one")
        if self.finish_loot_ratio > 0 and self.finish_battle_tap is None:
            raise ValueError(
                "finish_battle_tap is required when finish_loot_ratio is positive"
            )
        if (
            self.finish_battle_confirm_tap is not None
            and self.finish_battle_tap is None
        ):
            raise ValueError(
                "finish_battle_tap is required when a confirmation tap is configured"
            )
        if self.finish_check_interval_ms <= 0:
            raise ValueError("finish_check_interval_ms must be positive")
        if self.max_nexts_per_cycle <= 0:
            raise ValueError("max_nexts_per_cycle must be positive")
        if self.match_ready_timeout_ms <= 0:
            raise ValueError("match_ready_timeout_ms must be positive")
        if self.return_timeout_ms <= 0:
            raise ValueError("return_timeout_ms must be positive")
        if self.home_timeout_ms <= 0:
            raise ValueError("home_timeout_ms must be positive")
        if self.long_gesture_threshold_ms < 0:
            raise ValueError("long_gesture_threshold_ms cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "thresholds": self.thresholds.to_dict(),
            "loot_mode": self.loot_mode,
            "sum_threshold": self.sum_threshold,
            "variation": self.variation.to_dict(),
            "timing": self.timing.to_dict(),
            "next_button": (
                self.next_button.to_dict() if self.next_button is not None else None
            ),
            "return_tap": (
                self.return_tap.to_dict() if self.return_tap is not None else None
            ),
            "home_popup_dismiss_tap": (
                self.home_popup_dismiss_tap.to_dict()
                if self.home_popup_dismiss_tap is not None
                else None
            ),
            "finish_battle_tap": (
                self.finish_battle_tap.to_dict()
                if self.finish_battle_tap is not None
                else None
            ),
            "finish_battle_confirm_tap": (
                self.finish_battle_confirm_tap.to_dict()
                if self.finish_battle_confirm_tap is not None
                else None
            ),
            "finish_loot_ratio": self.finish_loot_ratio,
            "finish_check_interval_ms": self.finish_check_interval_ms,
            "max_nexts_per_cycle": self.max_nexts_per_cycle,
            "match_ready_timeout_ms": self.match_ready_timeout_ms,
            "return_timeout_ms": self.return_timeout_ms,
            "home_timeout_ms": self.home_timeout_ms,
            "long_gesture_threshold_ms": self.long_gesture_threshold_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FarmConfig:
        _validate_schema(value)
        next_raw = value.get("next_button")
        return_raw = value.get("return_tap")
        home_popup_dismiss_raw = value.get("home_popup_dismiss_tap")
        finish_raw = value.get("finish_battle_tap")
        finish_confirm_raw = value.get("finish_battle_confirm_tap")
        return cls(
            thresholds=LootThresholds.from_dict(value.get("thresholds", {})),
            loot_mode=value.get("loot_mode", "all"),
            sum_threshold=int(value.get("sum_threshold", 0)),
            variation=VariationConfig.from_dict(value.get("variation", {})),
            timing=TimingConfig.from_dict(value.get("timing", {})),
            next_button=Point.from_dict(next_raw) if next_raw else None,
            return_tap=Point.from_dict(return_raw) if return_raw else None,
            home_popup_dismiss_tap=(
                Point.from_dict(home_popup_dismiss_raw)
                if home_popup_dismiss_raw
                else None
            ),
            finish_battle_tap=Point.from_dict(finish_raw) if finish_raw else None,
            finish_battle_confirm_tap=(
                Point.from_dict(finish_confirm_raw) if finish_confirm_raw else None
            ),
            finish_loot_ratio=float(value.get("finish_loot_ratio", 0.0)),
            finish_check_interval_ms=int(
                value.get("finish_check_interval_ms", 2_000)
            ),
            max_nexts_per_cycle=int(value.get("max_nexts_per_cycle", 50)),
            match_ready_timeout_ms=int(value.get("match_ready_timeout_ms", 60_000)),
            return_timeout_ms=int(value.get("return_timeout_ms", 180_000)),
            home_timeout_ms=int(value.get("home_timeout_ms", 180_000)),
            long_gesture_threshold_ms=int(
                value.get("long_gesture_threshold_ms", 3_000)
            ),
        )


@dataclass(frozen=True, slots=True)
class LootReading:
    gold: int | None
    elixir: int | None
    dark: int | None = None


def _validate_delay(delay_ms: int) -> None:
    if delay_ms < 0:
        raise ValueError("action delay cannot be negative")


def _validate_schema(value: dict[str, Any]) -> None:
    version = int(value.get("schema_version", 0))
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema version {version}; expected {SCHEMA_VERSION}"
        )
