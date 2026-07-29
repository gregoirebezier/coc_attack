"""Serial-bound ADB client for screenshots, input, and device inspection."""

from __future__ import annotations

import re
import struct
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, TextIO

from PIL import Image, UnidentifiedImageError

from coc_farm2.models import AppBounds, ContactGroupAction, DeviceProfile
from coc_farm2.session import (
    flatten_contact_session,
    format_session_file,
    session_duration_ms,
)

_TAP_HOLD_FLOOR_MS = 25

Runner = Callable[..., subprocess.CompletedProcess[Any]]
PopenFactory = Callable[..., Any]
_GESTURE_TIMEOUT_MARGIN_S = 5.0
GESTURE_HELPER_REMOTE_PATH = "/data/local/tmp/coc-farm2-gesture.zip"
GESTURE_HELPER_CLASS = "coc.farm2.GestureInjector"
# Back-compat aliases
PINCH_HELPER_REMOTE_PATH = GESTURE_HELPER_REMOTE_PATH
PINCH_HELPER_CLASS = GESTURE_HELPER_CLASS


class AdbError(RuntimeError):
    """Base error for an ADB transport or response failure."""


class AdbCommandError(AdbError):
    def __init__(
        self,
        command: Sequence[str],
        returncode: int,
        output: str,
    ) -> None:
        self.command = tuple(command)
        self.returncode = returncode
        self.output = output
        detail = output.strip() or "no error output"
        super().__init__(f"ADB command failed with exit code {returncode}: {detail}")


class AdbParseError(AdbError):
    """Raised when Android returns output that cannot be trusted."""


@dataclass(frozen=True, slots=True)
class AdbDevice:
    serial: str
    state: str
    details: str = ""


@dataclass(frozen=True, slots=True)
class AppComponent:
    package: str
    activity: str

    @property
    def flattened(self) -> str:
        return f"{self.package}/{self.activity}"


@dataclass(frozen=True, slots=True)
class LockState:
    awake: bool
    keyguard_showing: bool

    @property
    def unlocked(self) -> bool:
        return self.awake and not self.keyguard_showing


@dataclass(frozen=True, slots=True)
class BatteryState:
    level_percent: int
    status: int
    powered_sources: tuple[str, ...]

    @property
    def charging(self) -> bool:
        return bool(self.powered_sources) and self.status in {2, 5}


@dataclass(frozen=True, slots=True)
class DisplayInfo:
    logical_width: int
    logical_height: int
    rotation: int
    app_bounds: AppBounds


@dataclass(frozen=True, slots=True)
class TouchDevice:
    path: str
    name: str
    x_min: int
    x_max: int
    y_min: int
    y_max: int

    def __post_init__(self) -> None:
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError("touch axes must have positive ranges")

    @property
    def raw_width(self) -> int:
        return self.x_max - self.x_min

    @property
    def raw_height(self) -> int:
        return self.y_max - self.y_min


@dataclass(frozen=True, slots=True)
class SafetyStatus:
    online: bool
    foreground: bool
    unlocked: bool
    logical_width: int
    logical_height: int
    rotation: int
    app_bounds: AppBounds | None
    app_version: str


class PersistentAdbShell:
    """
    Long-lived ``adb shell`` used for input injection.

    Feeding commands into one shell avoids paying process startup on every tap
    (often >1s per ``adb shell input …`` on some hosts).
    """

    def __init__(
        self,
        *,
        adb_path: str,
        serial: str,
        popen_factory: PopenFactory = subprocess.Popen,
        default_timeout_s: float = 30.0,
    ) -> None:
        self.default_timeout_s = default_timeout_s
        self._lock = threading.Lock()
        try:
            self._proc = popen_factory(
                [adb_path, "-s", serial, "shell"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as error:
            raise AdbError(f"ADB executable not found: {adb_path}") from error
        except OSError as error:
            raise AdbError(f"could not start persistent adb shell: {error}") from error
        if self._proc.stdin is None or self._proc.stdout is None:
            self.close()
            raise AdbError("persistent adb shell missing stdio pipes")

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def run(self, command: str, *, timeout_s: float | None = None) -> None:
        """Run one shell command and wait until it finishes (via echo marker)."""
        if not command.strip():
            raise ValueError("shell command is required")
        timeout = self.default_timeout_s if timeout_s is None else timeout_s
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        token = f"__coc_farm2_{uuid.uuid4().hex}__"
        # Run command then emit a marker so we know when it completed.
        # Use `;` so marker still prints if command fails.
        payload = f"{command}; echo {token}\n"
        with self._lock:
            if not self.alive:
                raise AdbError("persistent adb shell is not running")
            stdin = self._proc.stdin
            stdout = self._proc.stdout
            assert stdin is not None and stdout is not None
            try:
                stdin.write(payload)
                stdin.flush()
            except BrokenPipeError as error:
                raise AdbError("persistent adb shell stdin closed") from error
            self._wait_for_token(stdout, token, timeout_s=timeout)

    def _wait_for_token(
        self,
        stdout: TextIO,
        token: str,
        *,
        timeout_s: float,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.alive:
                raise AdbError("persistent adb shell exited while waiting")
            # readline blocks; use a short overall timeout via deadline checks
            # after each line. For long gestures this is fine.
            line = stdout.readline()
            if line == "":
                if not self.alive:
                    raise AdbError("persistent adb shell closed stdout")
                # rare: no data yet
                time.sleep(0.001)
                continue
            if token in line:
                return
        raise AdbError(
            f"persistent adb shell timed out after {timeout_s:g}s "
            "waiting for completion"
        )

    def close(self) -> None:
        with self._lock:
            if self._proc.poll() is not None:
                return
            stdin = self._proc.stdin
            try:
                if stdin is not None:
                    stdin.write("exit\n")
                    stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2.0)
            for stream_name in ("stdin", "stdout", "stderr"):
                stream = getattr(self._proc, stream_name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


class AdbClient:
    """Small serial-bound ADB interface with an injectable process boundary."""

    def __init__(
        self,
        serial: str,
        *,
        adb_path: str = "adb",
        runner: Runner = subprocess.run,
        popen_factory: PopenFactory = subprocess.Popen,
        timeout_s: float = 15.0,
    ) -> None:
        if not serial.strip():
            raise ValueError("ADB serial is required")
        if timeout_s <= 0:
            raise ValueError("ADB timeout must be positive")
        self.serial = serial
        self.adb_path = adb_path
        self._runner = runner
        self._popen_factory = popen_factory
        self.timeout_s = timeout_s
        self._gesture_helper_installed = False
        self._input_shell: PersistentAdbShell | None = None

    def list_devices(self) -> tuple[AdbDevice, ...]:
        output = self._run_text([self.adb_path, "devices", "-l"])
        devices: list[AdbDevice] = []
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("List of devices"):
                continue
            fields = stripped.split(maxsplit=2)
            if len(fields) < 2:
                continue
            devices.append(
                AdbDevice(
                    serial=fields[0],
                    state=fields[1],
                    details=fields[2] if len(fields) == 3 else "",
                )
            )
        return tuple(devices)

    def device_state(self) -> str | None:
        for device in self.list_devices():
            if device.serial == self.serial:
                return device.state
        return None

    def device_model(self) -> str:
        model = self.shell("getprop", "ro.product.model").strip()
        if not model:
            raise AdbParseError("Android did not report a device model")
        return model

    def android_api(self) -> int:
        output = self.shell("getprop", "ro.build.version.sdk").strip()
        try:
            api = int(output)
        except ValueError as error:
            raise AdbParseError(f"invalid Android API level: {output!r}") from error
        if api <= 0:
            raise AdbParseError(f"invalid Android API level: {api}")
        return api

    def resolve_activity(self, package: str) -> AppComponent:
        if not package.strip():
            raise ValueError("package is required")
        output = self.shell(
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            package,
        )
        for line in reversed(output.splitlines()):
            match = re.fullmatch(
                r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)",
                line.strip(),
            )
            if match is None:
                continue
            resolved_package, activity = match.groups()
            if activity.startswith("."):
                activity = f"{resolved_package}{activity}"
            return AppComponent(resolved_package, activity)
        raise AdbParseError(f"no launchable activity resolved for package {package!r}")

    def app_version(self, package: str) -> str:
        if not package.strip():
            raise ValueError("package is required")
        output = self.shell("dumpsys", "package", package)
        match = re.search(r"^\s*versionName=(\S+)\s*$", output, re.MULTILINE)
        if match is None:
            raise AdbParseError(f"no versionName reported for package {package!r}")
        return match.group(1)

    def foreground_component(self) -> AppComponent:
        output = self.shell("dumpsys", "window", "displays")
        component_pattern = re.compile(r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)")
        for marker in ("mCurrentFocus=", "mFocusedApp="):
            for line in output.splitlines():
                if marker not in line:
                    continue
                match = component_pattern.search(line)
                if match is None:
                    continue
                package, activity = match.groups()
                if activity.startswith("."):
                    activity = f"{package}{activity}"
                return AppComponent(package, activity)
        raise AdbParseError("Android did not report a foreground activity")

    def lock_state(self) -> LockState:
        policy = self.shell("dumpsys", "window", "policy")
        power = self.shell("dumpsys", "power")

        keyguard_values = re.findall(
            r"^\s*(?:showing|mIsShowing|isKeyguardShowing)"
            r"=(true|false)\s*$",
            policy,
            re.MULTILINE,
        )
        screen_match = re.search(
            r"^\s*screenState=(\S+)\s*$",
            policy,
            re.MULTILINE,
        )
        interactive_match = re.search(
            r"^\s*interactiveState=(\S+)\s*$",
            policy,
            re.MULTILINE,
        )
        wakefulness_match = re.search(
            r"^\s*mWakefulness=(\S+)\s*$",
            power,
            re.MULTILINE,
        )
        if (
            not keyguard_values
            or screen_match is None
            or interactive_match is None
            or wakefulness_match is None
        ):
            raise AdbParseError("Android lock or wakefulness state was incomplete")

        awake = (
            screen_match.group(1) == "SCREEN_STATE_ON"
            and interactive_match.group(1) == "INTERACTIVE_STATE_AWAKE"
            and wakefulness_match.group(1) == "Awake"
        )
        return LockState(
            awake=awake,
            keyguard_showing="true" in keyguard_values,
        )

    def battery_state(self) -> BatteryState:
        output = self.shell("dumpsys", "battery")
        power_matches = re.findall(
            r"^\s*(AC|USB|Wireless|Dock) powered: (true|false)\s*$",
            output,
            re.MULTILINE,
        )
        status_match = re.search(r"^\s*status:\s*(\d+)\s*$", output, re.MULTILINE)
        level_match = re.search(r"^\s*level:\s*(\d+)\s*$", output, re.MULTILINE)
        scale_match = re.search(r"^\s*scale:\s*(\d+)\s*$", output, re.MULTILINE)
        if (
            len(power_matches) != 4
            or status_match is None
            or level_match is None
            or scale_match is None
        ):
            raise AdbParseError("Android battery state was incomplete")

        scale = int(scale_match.group(1))
        if scale <= 0:
            raise AdbParseError("Android reported an invalid battery scale")
        level_percent = round(int(level_match.group(1)) * 100 / scale)
        if not 0 <= level_percent <= 100:
            raise AdbParseError("Android reported an invalid battery level")

        return BatteryState(
            level_percent=level_percent,
            status=int(status_match.group(1)),
            powered_sources=tuple(
                source for source, powered in power_matches if powered == "true"
            ),
        )

    def display_info(self) -> DisplayInfo:
        output = self.shell("dumpsys", "window", "displays")
        dimensions_match = re.search(r"\bcur=(\d+)x(\d+)\b", output)
        bounds_match = re.search(
            r"\bmAppBounds=Rect\(\s*(\d+),\s*(\d+)\s*-\s*"
            r"(\d+),\s*(\d+)\s*\)",
            output,
        )
        frames_match = re.search(
            r"\bDisplayFrames\s+w=(\d+)\s+h=(\d+)\s+r=([0-3])\b",
            output,
        )
        if dimensions_match is None or bounds_match is None or frames_match is None:
            raise AdbParseError("Android display state was incomplete")

        logical_width, logical_height = (
            int(value) for value in dimensions_match.groups()
        )
        frame_width, frame_height, rotation = (
            int(value) for value in frames_match.groups()
        )
        if (logical_width, logical_height) != (frame_width, frame_height):
            raise AdbParseError(
                "Android reported conflicting logical display dimensions"
            )
        try:
            app_bounds = AppBounds(*(int(value) for value in bounds_match.groups()))
        except ValueError as error:
            raise AdbParseError(
                f"Android reported invalid app bounds: {error}"
            ) from error

        return DisplayInfo(
            logical_width=logical_width,
            logical_height=logical_height,
            rotation=rotation,
            app_bounds=app_bounds,
        )

    def touch_device(self, name: str = "sec_touchscreen") -> TouchDevice:
        if not name.strip():
            raise ValueError("touch device name is required")
        output = self.shell("getevent", "-pl")
        sections = re.split(r"(?=^add device \d+:)", output, flags=re.MULTILINE)
        for section in sections:
            path_match = re.search(
                r"^add device \d+:\s+(\S+)\s*$",
                section,
                re.MULTILINE,
            )
            name_match = re.search(
                r'^\s*name:\s+"([^"]+)"\s*$',
                section,
                re.MULTILINE,
            )
            if path_match is None or name_match is None or name_match.group(1) != name:
                continue

            x_match = re.search(
                r"(?:ABS_MT_POSITION_X|0035)\s*:\s*"
                r"value\s+-?\d+,\s*min\s+(-?\d+),\s*max\s+(-?\d+)",
                section,
            )
            y_match = re.search(
                r"(?:ABS_MT_POSITION_Y|0036)\s*:\s*"
                r"value\s+-?\d+,\s*min\s+(-?\d+),\s*max\s+(-?\d+)",
                section,
            )
            if x_match is None or y_match is None:
                raise AdbParseError(
                    f"touch device {name!r} did not report position axes"
                )
            try:
                return TouchDevice(
                    path=path_match.group(1),
                    name=name,
                    x_min=int(x_match.group(1)),
                    x_max=int(x_match.group(2)),
                    y_min=int(y_match.group(1)),
                    y_max=int(y_match.group(2)),
                )
            except ValueError as error:
                raise AdbParseError(
                    f"touch device {name!r} reported invalid axes: {error}"
                ) from error
        raise AdbParseError(f"touch device {name!r} was not found")

    def bring_to_front(self, package: str, activity: str) -> None:
        if not package.strip() or not activity.strip():
            raise ValueError("package and activity are required")
        output = self.shell(
            "am",
            "start",
            "--activity-single-top",
            "-n",
            f"{package}/{activity}",
        )
        if re.search(r"^\s*(?:Error|Exception):", output, re.MULTILINE):
            raise AdbParseError(
                f"Android could not bring {package}/{activity} to front: "
                f"{output.strip()}"
            )

    def open_input_shell(self) -> None:
        """Open (or reopen) the persistent shell used for taps/paths/pinches."""
        if self._input_shell is not None and self._input_shell.alive:
            return
        self.close_input_shell()
        self._input_shell = PersistentAdbShell(
            adb_path=self.adb_path,
            serial=self.serial,
            popen_factory=self._popen_factory,
            default_timeout_s=max(self.timeout_s, 30.0),
        )

    def close_input_shell(self) -> None:
        if self._input_shell is None:
            return
        self._input_shell.close()
        self._input_shell = None

    @contextmanager
    def _without_input_shell(self) -> Iterator[None]:
        """
        Temporarily release the persistent shell.

        Concurrent ``adb shell`` + ``exec-out screencap`` / dumpsys often hangs
        on USB devices. Drop the input session around those calls, then reopen
        if it was warm.
        """
        was_open = self._input_shell is not None and self._input_shell.alive
        if was_open:
            self.close_input_shell()
        try:
            yield
        finally:
            if was_open:
                self.open_input_shell()

    def _input_run(self, command: str, *, timeout_s: float | None = None) -> None:
        """Run an input-related shell command on the persistent session."""
        self.open_input_shell()
        assert self._input_shell is not None
        try:
            self._input_shell.run(command, timeout_s=timeout_s)
        except AdbError:
            # One retry with a fresh shell (USB blip / shell died).
            self.close_input_shell()
            self.open_input_shell()
            assert self._input_shell is not None
            self._input_shell.run(command, timeout_s=timeout_s)

    def tap(self, x: int, y: int, duration_ms: int = 0) -> None:
        if x < 0 or y < 0:
            raise ValueError("tap coordinates cannot be negative")
        if duration_ms < 0:
            raise ValueError("tap duration cannot be negative")
        # Fast path via persistent shell: `input swipe` with zero travel holds.
        # Avoids app_process/JVM startup on every troop/spell tap.
        hold_ms = max(duration_ms, _TAP_HOLD_FLOOR_MS)
        timeout_s = max(self.timeout_s, hold_ms / 1000 + _GESTURE_TIMEOUT_MARGIN_S)
        self._input_run(
            f"input swipe {x} {y} {x} {y} {hold_ms}",
            timeout_s=timeout_s,
        )

    def ui_hierarchy(self) -> str:
        """Dump Android accessibility state for system-dialog detection."""
        remote = "/sdcard/coc-farm2-window.xml"
        self.shell(
            "uiautomator",
            "dump",
            remote,
            timeout_s=max(self.timeout_s, 10.0),
        )
        encoded = self.exec_out("cat", remote, timeout_s=max(self.timeout_s, 10.0))
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AdbParseError("Android UI hierarchy was not UTF-8") from error

    def dismiss_foreign_dialog(self, expected_package: str) -> str | None:
        """Press Back only when accessibility exposes UI outside the game."""
        if not expected_package.strip():
            raise ValueError("expected package is required")
        hierarchy = self.ui_hierarchy()
        try:
            root = ET.fromstring(hierarchy)
        except ET.ParseError as error:
            raise AdbParseError("Android UI hierarchy was invalid XML") from error

        foreign = [
            node
            for node in root.iter("node")
            if node.attrib.get("package") and node.attrib["package"] != expected_package
        ]
        if not foreign:
            return None

        visible_text = next(
            (text for node in foreign if (text := node.attrib.get("text", "").strip())),
            None,
        )
        package = foreign[0].attrib["package"]
        if package == "com.samsung.android.cidmanager":
            self._input_run(f"am force-stop {package}")
        else:
            self._input_run("input keyevent KEYCODE_BACK")
        return visible_text or package

    def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int,
        *,
        points: Sequence[tuple[int, int]] | None = None,
        times_ms: Sequence[int] | None = None,
    ) -> None:
        if min(x1, y1, x2, y2) < 0:
            raise ValueError("swipe coordinates cannot be negative")
        if duration_ms <= 0:
            raise ValueError("swipe duration must be positive")
        path = list(points) if points else [(x1, y1), (x2, y2)]
        if len(path) < 2:
            path = [(x1, y1), (x2, y2)]

        # Multi-point / timed path: MotionEvent helper (still via persistent shell).
        use_helper = self._gesture_helper_installed and (
            len(path) > 2 or (times_ms is not None and len(times_ms) == len(path))
        )
        if use_helper:
            if times_ms and len(times_ms) == len(path):
                samples = tuple(
                    (x, y, int(t)) for (x, y), t in zip(path, times_ms, strict=True)
                )
            else:
                last = max(duration_ms, len(path) - 1)
                samples = tuple(
                    (x, y, round(i * last / (len(path) - 1)))
                    for i, (x, y) in enumerate(path)
                )
            self.inject_path_timed(samples)
            return

        timeout_s = max(
            self.timeout_s,
            duration_ms / 1000 + _GESTURE_TIMEOUT_MARGIN_S,
        )
        self._input_run(
            "input swipe "
            f"{path[0][0]} {path[0][1]} {path[-1][0]} {path[-1][1]} {duration_ms}",
            timeout_s=timeout_s,
        )

    def inject_path_timed(
        self,
        samples: Sequence[tuple[int, int, int]],
    ) -> None:
        """Inject a single-finger path: each sample is (x, y, t_ms_from_down)."""
        if len(samples) < 1:
            raise ValueError("path requires at least one sample")
        if not self._gesture_helper_installed:
            raise AdbError("gesture helper is not installed for this ADB session")
        # Ensure non-decreasing times and a real hold for single-point taps.
        normalized: list[tuple[int, int, int]] = []
        last_t = -1
        for x, y, t in samples:
            t_ms = max(int(t), last_t if last_t >= 0 else 0)
            if last_t >= 0 and t_ms < last_t:
                t_ms = last_t
            normalized.append((int(x), int(y), t_ms))
            last_t = t_ms
        if len(normalized) == 1:
            x, y, t0 = normalized[0]
            normalized = [(x, y, 0), (x, y, max(t0, _TAP_HOLD_FLOOR_MS))]
        elif normalized[-1][2] <= 0:
            x, y, _ = normalized[-1]
            normalized[-1] = (x, y, _TAP_HOLD_FLOOR_MS)

        args = " ".join(f"{x} {y} {t}" for x, y, t in normalized)
        duration_ms = normalized[-1][2]
        timeout_s = max(
            self.timeout_s,
            duration_ms / 1000 + _GESTURE_TIMEOUT_MARGIN_S,
        )
        self._input_run(
            f"CLASSPATH={GESTURE_HELPER_REMOTE_PATH} "
            f"app_process / {GESTURE_HELPER_CLASS} path {args}",
            timeout_s=timeout_s,
        )

    def inject_contacts(
        self, action: ContactGroupAction, profile: DeviceProfile
    ) -> None:
        """Inject one contact group (single-bout helper). Prefer session batching."""
        self.inject_contact_session((action,), profile)

    def inject_contact_session(
        self,
        groups: Sequence[ContactGroupAction],
        profile: DeviceProfile,
    ) -> None:
        """
        Inject one or more contact groups in a single on-device JVM.

        Inter-group gaps are slept inside GestureInjector — avoids paying
        ``app_process`` startup once per group (the main replay slowdown).
        """
        if profile.serial != self.serial:
            raise ValueError(
                f"contact profile is bound to {profile.serial}, not {self.serial}"
            )
        if not groups:
            raise ValueError("contact session requires at least one group")
        for group in groups:
            for sample in group.samples:
                if not profile.app_bounds.contains(sample.x, sample.y):
                    raise ValueError("contact coordinates must stay inside app bounds")
        if not self._gesture_helper_installed:
            raise AdbError("gesture helper is not installed for this ADB session")

        events = flatten_contact_session(groups)
        remote = "/data/local/tmp/coc-farm2-session.txt"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".session.txt", delete=False
        ) as handle:
            handle.write(format_session_file(events))
            local_path = Path(handle.name)
        try:
            with self._without_input_shell():
                self._run_text(
                    [
                        self.adb_path,
                        "-s",
                        self.serial,
                        "push",
                        str(local_path),
                        remote,
                    ]
                )
        finally:
            local_path.unlink(missing_ok=True)

        timeout_s = max(
            self.timeout_s,
            session_duration_ms(groups) / 1000 + _GESTURE_TIMEOUT_MARGIN_S,
        )
        self._input_run(
            f"CLASSPATH={GESTURE_HELPER_REMOTE_PATH} "
            f"app_process / {GESTURE_HELPER_CLASS} session {remote}",
            timeout_s=timeout_s,
        )

    def install_pinch_helper(self, helper_path: Path) -> None:
        """Install the on-device path+pinch MotionEvent helper."""
        if not helper_path.is_file():
            raise ValueError(f"gesture helper does not exist: {helper_path}")
        self._run_text(
            [
                self.adb_path,
                "-s",
                self.serial,
                "push",
                str(helper_path),
                GESTURE_HELPER_REMOTE_PATH,
            ]
        )
        self._gesture_helper_installed = True

    def safety_status(self, expected: DeviceProfile) -> SafetyStatus:
        unavailable = SafetyStatus(
            online=False,
            foreground=False,
            unlocked=False,
            logical_width=0,
            logical_height=0,
            rotation=-1,
            app_bounds=None,
            app_version="",
        )
        if self.serial != expected.serial:
            return unavailable
        try:
            if self.device_state() != "device":
                return unavailable
        except AdbError:
            return unavailable

        try:
            foreground = self.foreground_component() == AppComponent(
                expected.package,
                expected.activity,
            )
        except AdbError:
            foreground = False
        try:
            unlocked = self.lock_state().unlocked
        except AdbError:
            unlocked = False
        try:
            display = self.display_info()
            logical_width = display.logical_width
            logical_height = display.logical_height
            rotation = display.rotation
            app_bounds = display.app_bounds
        except AdbError:
            logical_width = 0
            logical_height = 0
            rotation = -1
            app_bounds = None
        try:
            app_version = self.app_version(expected.package)
        except AdbError:
            app_version = ""

        return SafetyStatus(
            online=True,
            foreground=foreground,
            unlocked=unlocked,
            logical_width=logical_width,
            logical_height=logical_height,
            rotation=rotation,
            app_bounds=app_bounds,
            app_version=app_version,
        )

    def screenshot(self) -> Image.Image:
        """
        Capture the display as RGB.

        Prefers raw ``screencap`` (~2× faster than PNG on USB). Falls back to
        ``screencap -p`` if the raw buffer cannot be parsed.
        """
        # exec_out releases the input shell (concurrent adb + shell hangs USB).
        timeout_s = max(self.timeout_s, 20.0)
        raw = self.exec_out("screencap", timeout_s=timeout_s)
        parsed = _parse_raw_screencap(raw)
        if parsed is not None:
            return parsed
        encoded = self.exec_out("screencap", "-p", timeout_s=timeout_s)
        try:
            with Image.open(BytesIO(encoded)) as source:
                screenshot = source.convert("RGB")
                screenshot.load()
        except (UnidentifiedImageError, OSError) as error:
            raise AdbParseError(
                "Android screencap did not return a valid image"
            ) from error
        return screenshot

    @contextmanager
    def getevent_lines(self, device_path: str) -> Iterator[Iterator[str]]:
        if re.fullmatch(r"/dev/input/event\d+", device_path) is None:
            raise ValueError("touch event path must look like /dev/input/event<number>")
        command = [
            self.adb_path,
            "-s",
            self.serial,
            "shell",
            "getevent",
            "-lt",
            device_path,
        ]
        try:
            process = self._popen_factory(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as error:
            raise AdbError(f"ADB executable not found: {self.adb_path}") from error
        except OSError as error:
            raise AdbError(f"could not start ADB getevent: {error}") from error

        stdout = getattr(process, "stdout", None)
        if stdout is None:
            self._stop_streaming_process(process)
            raise AdbError("ADB getevent did not expose a stdout stream")
        try:
            yield iter(stdout)
        finally:
            self._stop_streaming_process(process)

    @staticmethod
    def _stop_streaming_process(process: Any) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
        for stream_name in ("stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            if stream is not None:
                stream.close()

    def shell(self, *arguments: str, timeout_s: float | None = None) -> str:
        if not arguments:
            raise ValueError("shell command is required")
        command = [
            self.adb_path,
            "-s",
            self.serial,
            "shell",
            *arguments,
        ]
        # One-shot shell/dumpsys must not race the persistent input session.
        with self._without_input_shell():
            return self._run_text(command, timeout_s=timeout_s)

    def exec_out(self, *arguments: str, timeout_s: float | None = None) -> bytes:
        if not arguments:
            raise ValueError("exec-out command is required")
        command = [
            self.adb_path,
            "-s",
            self.serial,
            "exec-out",
            *arguments,
        ]
        with self._without_input_shell():
            completed = self._invoke(command, binary=True, timeout_s=timeout_s)
        if not isinstance(completed.stdout, bytes):
            raise AdbParseError("ADB binary command returned text output")
        return completed.stdout

    def _run_text(
        self,
        command: Sequence[str],
        *,
        timeout_s: float | None = None,
    ) -> str:
        completed = self._invoke(command, binary=False, timeout_s=timeout_s)
        if not isinstance(completed.stdout, str):
            raise AdbParseError("ADB text command returned binary output")
        return completed.stdout.rstrip("\r\n")

    def _invoke(
        self,
        command: Sequence[str],
        *,
        binary: bool,
        timeout_s: float | None,
    ) -> subprocess.CompletedProcess[Any]:
        timeout = self.timeout_s if timeout_s is None else timeout_s
        if timeout <= 0:
            raise ValueError("ADB timeout must be positive")
        try:
            completed = self._runner(
                list(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
                text=not binary,
            )
        except FileNotFoundError as error:
            raise AdbError(f"ADB executable not found: {self.adb_path}") from error
        except subprocess.TimeoutExpired as error:
            raise AdbError(f"ADB command timed out after {timeout:g}s") from error

        if completed.returncode != 0:
            stderr = completed.stderr
            stdout = completed.stdout
            if isinstance(stderr, bytes):
                error_output = stderr.decode(errors="replace")
            else:
                error_output = stderr or ""
            if not error_output:
                if isinstance(stdout, bytes):
                    error_output = stdout.decode(errors="replace")
                else:
                    error_output = stdout or ""
            raise AdbCommandError(command, completed.returncode, error_output)
        return completed


def _parse_raw_screencap(raw: bytes) -> Image.Image | None:
    """
    Decode Android raw screencap (RGBA8888).

    Header is 12 bytes historically, or 16 on Android 13+. Payload size is used
    to locate the pixel buffer so both layouts work.
    """
    if len(raw) < 12:
        return None
    try:
        width, height, _fmt = struct.unpack_from("<III", raw, 0)
    except struct.error:
        return None
    if width <= 0 or height <= 0 or width > 10_000 or height > 10_000:
        return None
    payload = width * height * 4
    if len(raw) < payload:
        return None
    header = len(raw) - payload
    if header not in {12, 16}:
        # Some devices add padding; still accept if payload fits.
        if header < 12 or header > 32:
            return None
    try:
        return Image.frombytes(
            "RGBA",
            (width, height),
            raw[header : header + payload],
            "raw",
            "RGBA",
        ).convert("RGB")
    except (ValueError, TypeError):
        return None
