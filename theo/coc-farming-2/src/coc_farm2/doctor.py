"""Device profile verification and lock-in."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from coc_farm2.adb import (
    AdbClient,
    AdbError,
    AppComponent,
    DisplayInfo,
    TouchDevice,
)
from coc_farm2.models import AppBounds, DeviceProfile

DEFAULT_SERIAL = "RF8WA0586AB"
COC_PACKAGE = "com.supercell.clashofclans"
COC_ACTIVITY = "com.supercell.titan.GameApp"


@dataclass(frozen=True, slots=True)
class DoctorExpectations:
    serial: str = DEFAULT_SERIAL
    model: str = "SM-A145F"
    android_api: int = 34
    package: str = COC_PACKAGE
    activity: str = COC_ACTIVITY
    app_version: str | None = None
    logical_width: int = 2408
    logical_height: int = 1080
    raw_width: int = 1080
    raw_height: int = 2408
    rotation: int = 1
    touch_name: str = "sec_touchscreen"
    app_bounds: AppBounds = field(default_factory=lambda: AppBounds(64, 0, 2273, 1080))
    require_foreground: bool = True
    require_unlocked: bool = True
    require_charging: bool = True

    def __post_init__(self) -> None:
        if not all(
            (
                self.serial,
                self.model,
                self.package,
                self.activity,
                self.touch_name,
            )
        ):
            raise ValueError("doctor identity expectations cannot be empty")
        if self.android_api <= 0:
            raise ValueError("expected Android API must be positive")
        if (
            min(
                self.logical_width,
                self.logical_height,
                self.raw_width,
                self.raw_height,
            )
            <= 0
        ):
            raise ValueError("expected device dimensions must be positive")
        if self.rotation not in {0, 1, 2, 3}:
            raise ValueError("expected rotation must be 0, 1, 2, or 3")


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    passed: bool
    detail: str
    critical: bool = True


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]
    profile: DeviceProfile | None

    @property
    def ok(self) -> bool:
        return all(check.passed or not check.critical for check in self.checks)

    def check(self, name: str) -> DoctorCheck:
        for item in self.checks:
            if item.name == name:
                return item
        raise KeyError(f"doctor check {name!r} does not exist")


def _observe[T](
    checks: list[DoctorCheck],
    name: str,
    operation: Callable[[], T],
    validate: Callable[[T], tuple[bool, str]],
    *,
    critical: bool = True,
) -> T | None:
    try:
        value = operation()
    except AdbError as error:
        checks.append(
            DoctorCheck(
                name=name,
                passed=False,
                detail=f"{type(error).__name__}: {error}",
                critical=critical,
            )
        )
        return None
    passed, detail = validate(value)
    checks.append(
        DoctorCheck(name=name, passed=passed, detail=detail, critical=critical)
    )
    return value


def run_doctor(
    client: AdbClient,
    expectations: DoctorExpectations | None = None,
) -> DoctorReport:
    expected = expectations or DoctorExpectations()
    checks: list[DoctorCheck] = []

    state = _observe(
        checks,
        "device-online",
        client.device_state,
        lambda value: (
            value == "device",
            f"state={value!r}" if value is not None else "device not listed",
        ),
    )
    if state != "device":
        return DoctorReport(tuple(checks), None)

    model = _observe(
        checks,
        "model",
        client.device_model,
        lambda value: (value == expected.model, f"model={value}"),
    )
    api = _observe(
        checks,
        "android-api",
        client.android_api,
        lambda value: (value == expected.android_api, f"api={value}"),
    )
    component = _observe(
        checks,
        "activity",
        lambda: client.resolve_activity(expected.package),
        lambda value: (
            value == AppComponent(expected.package, expected.activity),
            f"component={value.flattened}",
        ),
    )
    version = _observe(
        checks,
        "app-version",
        lambda: client.app_version(expected.package),
        lambda value: (
            expected.app_version is None or value == expected.app_version,
            f"version={value}",
        ),
    )
    display = _observe(
        checks,
        "display",
        client.display_info,
        lambda value: _validate_display(value, expected),
    )
    touch = _observe(
        checks,
        "touchscreen",
        lambda: client.touch_device(expected.touch_name),
        lambda value: _validate_touch(value, expected),
    )
    lock = _observe(
        checks,
        "unlocked",
        client.lock_state,
        lambda value: (
            (not expected.require_unlocked) or value.unlocked,
            f"unlocked={value.unlocked}",
        ),
    )
    battery = _observe(
        checks,
        "charging",
        client.battery_state,
        lambda value: (
            (not expected.require_charging) or value.charging,
            f"charging={value.charging} level={value.level_percent}%",
        ),
        critical=False,
    )
    foreground = _observe(
        checks,
        "foreground",
        client.foreground_component,
        lambda value: (
            (not expected.require_foreground)
            or value == AppComponent(expected.package, expected.activity),
            f"foreground={value.flattened}",
        ),
    )
    _observe(
        checks,
        "screenshot",
        client.screenshot,
        lambda image: (image.width > 0 and image.height > 0, f"{image.size}"),
    )

    del lock, battery, foreground  # used for side-effect checks only

    profile: DeviceProfile | None = None
    if (
        model is not None
        and api is not None
        and component is not None
        and version is not None
        and display is not None
        and touch is not None
        and all(check.passed or not check.critical for check in checks)
    ):
        profile = DeviceProfile(
            serial=expected.serial,
            model=model,
            android_api=api,
            package=component.package,
            activity=component.activity,
            app_version=version,
            logical_width=display.logical_width,
            logical_height=display.logical_height,
            raw_width=touch.raw_width,
            raw_height=touch.raw_height,
            rotation=display.rotation,
            touch_device=touch.path,
            app_bounds=display.app_bounds,
        )

    return DoctorReport(tuple(checks), profile)


def _validate_display(
    display: DisplayInfo,
    expected: DoctorExpectations,
) -> tuple[bool, str]:
    ok = (
        display.logical_width == expected.logical_width
        and display.logical_height == expected.logical_height
        and display.rotation == expected.rotation
        and display.app_bounds == expected.app_bounds
    )
    detail = (
        f"{display.logical_width}x{display.logical_height} "
        f"rot={display.rotation} bounds={display.app_bounds}"
    )
    return ok, detail


def _validate_touch(
    touch: TouchDevice,
    expected: DoctorExpectations,
) -> tuple[bool, str]:
    ok = (
        touch.name == expected.touch_name
        and touch.raw_width == expected.raw_width
        and touch.raw_height == expected.raw_height
    )
    detail = f"{touch.path} name={touch.name} raw={touch.raw_width}x{touch.raw_height}"
    return ok, detail
