"""Contact-group timeline parse + MotionEvent inject argv shape."""

from __future__ import annotations

from coc_farm2.adb import AdbClient
from coc_farm2.models import (
    AppBounds,
    ContactGroupAction,
    ContactSample,
    DeviceProfile,
    Macro,
    TimingConfig,
)
from coc_farm2.recording import parse_getevent_trace
from coc_farm2.timing import apply_timing


def _profile() -> DeviceProfile:
    return DeviceProfile(
        serial="X",
        model="M",
        android_api=34,
        package="p",
        activity="a",
        app_version="1",
        logical_width=2408,
        logical_height=1080,
        raw_width=1080,
        raw_height=2408,
        rotation=1,
        touch_device="/dev/input/event1",
        app_bounds=AppBounds(64, 0, 2273, 1080),
    )


def _tap_group(
    delay_ms: int,
    x: int,
    y: int,
    *,
    hold_ms: int = 50,
) -> ContactGroupAction:
    return ContactGroupAction(
        delay_ms=delay_ms,
        samples=(
            ContactSample(t_ms=0, finger_id=0, x=x, y=y, phase="down"),
            ContactSample(t_ms=hold_ms, finger_id=0, x=x, y=y, phase="up"),
        ),
    )


def test_parse_simple_tap_is_contact_group() -> None:
    profile = _profile()
    trace = """
[   1.000000] EV_ABS       ABS_MT_TRACKING_ID   00000001
[   1.000000] EV_ABS       ABS_MT_POSITION_X    00000064
[   1.000000] EV_ABS       ABS_MT_POSITION_Y    000000c8
[   1.000000] EV_SYN       SYN_REPORT           00000000
[   1.050000] EV_ABS       ABS_MT_TRACKING_ID   ffffffff
[   1.050000] EV_SYN       SYN_REPORT           00000000
"""
    actions = parse_getevent_trace(trace, profile)
    assert len(actions) == 1
    assert isinstance(actions[0], ContactGroupAction)
    assert actions[0].finger_count == 1
    assert actions[0].duration_ms == 50
    phases = [s.phase for s in actions[0].samples]
    assert phases[0] == "down"
    assert phases[-1] == "up"


def test_parse_preserves_long_hold_duration() -> None:
    profile = _profile()
    trace = """
[   1.000000] EV_ABS       ABS_MT_TRACKING_ID   00000001
[   1.000000] EV_ABS       ABS_MT_POSITION_X    00000064
[   1.000000] EV_ABS       ABS_MT_POSITION_Y    000000c8
[   1.000000] EV_SYN       SYN_REPORT           00000000
[   4.380000] EV_ABS       ABS_MT_TRACKING_ID   ffffffff
[   4.380000] EV_SYN       SYN_REPORT           00000000
"""
    actions = parse_getevent_trace(trace, profile)
    assert isinstance(actions[0], ContactGroupAction)
    assert actions[0].duration_ms == 3380


def test_parse_two_finger_group_keeps_both_fingers() -> None:
    profile = _profile()
    trace = """
[   1.000000] EV_ABS       ABS_MT_SLOT          00000000
[   1.000000] EV_ABS       ABS_MT_TRACKING_ID   00000001
[   1.000000] EV_ABS       ABS_MT_POSITION_X    00000100
[   1.000000] EV_ABS       ABS_MT_POSITION_Y    00000200
[   1.000000] EV_SYN       SYN_REPORT           00000000
[   1.010000] EV_ABS       ABS_MT_SLOT          00000001
[   1.010000] EV_ABS       ABS_MT_TRACKING_ID   00000002
[   1.010000] EV_ABS       ABS_MT_POSITION_X    00000300
[   1.010000] EV_ABS       ABS_MT_POSITION_Y    00000600
[   1.010000] EV_SYN       SYN_REPORT           00000000
[   1.250000] EV_ABS       ABS_MT_SLOT          00000001
[   1.250000] EV_ABS       ABS_MT_TRACKING_ID   ffffffff
[   1.250000] EV_ABS       ABS_MT_SLOT          00000000
[   1.250000] EV_ABS       ABS_MT_TRACKING_ID   ffffffff
[   1.250000] EV_SYN       SYN_REPORT           00000000
"""
    actions = parse_getevent_trace(trace, profile)
    assert len(actions) == 1
    assert isinstance(actions[0], ContactGroupAction)
    assert actions[0].finger_count == 2
    assert actions[0].duration_ms == 250


def test_apply_timing_preserves_hold_and_keeps_gaps_when_scale_zero() -> None:
    macro = Macro(
        name="t",
        profile=_profile(),
        actions=(
            _tap_group(0, 10, 10, hold_ms=2000),
            _tap_group(500, 20, 20, hold_ms=100),
        ),
    )
    out = apply_timing(macro, TimingConfig(delay_scale=0.0))
    assert isinstance(out.actions[0], ContactGroupAction)
    assert out.actions[0].duration_ms == 2000
    assert out.actions[1].delay_ms == 500  # not stripped


def test_inject_contacts_uses_session_helper() -> None:
    commands: list[str] = []

    class _Shell:
        alive = True

        def run(self, command: str, *, timeout_s: float | None = None) -> None:
            commands.append(command)

        def close(self) -> None:
            return None

    profile = DeviceProfile(
        serial="SERIAL",
        model="M",
        android_api=34,
        package="p",
        activity="a",
        app_version="1",
        logical_width=100,
        logical_height=100,
        raw_width=100,
        raw_height=100,
        rotation=0,
        touch_device="/dev/input/event0",
        app_bounds=AppBounds(0, 0, 100, 100),
    )
    action = ContactGroupAction(
        delay_ms=0,
        samples=(
            ContactSample(0, 0, 10, 20, "down"),
            ContactSample(100, 0, 10, 20, "up"),
            ContactSample(0, 1, 30, 40, "down"),
            ContactSample(100, 1, 30, 40, "up"),
            ContactSample(10, 2, 50, 60, "down"),
            ContactSample(80, 2, 50, 60, "up"),
        ),
    )
    from contextlib import nullcontext

    client = AdbClient("SERIAL")
    client._gesture_helper_installed = True
    client._input_shell = _Shell()  # type: ignore[assignment]
    client._without_input_shell = lambda: nullcontext()  # type: ignore[method-assign]
    client._run_text = lambda command, **_k: ""  # type: ignore[method-assign]
    client.inject_contacts(action, profile)
    assert len(commands) == 1
    assert " session " in commands[0]
    assert "app_process" in commands[0]
