"""Multi-finger getevent traces become contact groups (no pinch/burst classify)."""

from __future__ import annotations

from coc_farm2.models import AppBounds, ContactGroupAction, DeviceProfile
from coc_farm2.recording import parse_getevent_trace


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


def _two_finger_trace(
    *,
    x1_start: int,
    y1_start: int,
    x1_end: int,
    y1_end: int,
    x2_start: int,
    y2_start: int,
    x2_end: int,
    y2_end: int,
    duration_s: float = 0.25,
) -> str:
    mid = duration_s / 2
    end = duration_s
    return f"""
[   1.000000] EV_ABS       ABS_MT_SLOT          00000000
[   1.000000] EV_ABS       ABS_MT_TRACKING_ID   00000001
[   1.000000] EV_ABS       ABS_MT_POSITION_X    {x1_start:08x}
[   1.000000] EV_ABS       ABS_MT_POSITION_Y    {y1_start:08x}
[   1.000000] EV_SYN       SYN_REPORT           00000000
[   1.010000] EV_ABS       ABS_MT_SLOT          00000001
[   1.010000] EV_ABS       ABS_MT_TRACKING_ID   00000002
[   1.010000] EV_ABS       ABS_MT_POSITION_X    {x2_start:08x}
[   1.010000] EV_ABS       ABS_MT_POSITION_Y    {y2_start:08x}
[   1.010000] EV_SYN       SYN_REPORT           00000000
[   {1.0 + mid:.6f}] EV_ABS       ABS_MT_SLOT          00000000
[   {1.0 + mid:.6f}] EV_ABS       ABS_MT_POSITION_X    {x1_end:08x}
[   {1.0 + mid:.6f}] EV_ABS       ABS_MT_POSITION_Y    {y1_end:08x}
[   {1.0 + mid:.6f}] EV_ABS       ABS_MT_SLOT          00000001
[   {1.0 + mid:.6f}] EV_ABS       ABS_MT_POSITION_X    {x2_end:08x}
[   {1.0 + mid:.6f}] EV_ABS       ABS_MT_POSITION_Y    {y2_end:08x}
[   {1.0 + mid:.6f}] EV_SYN       SYN_REPORT           00000000
[   {1.0 + end:.6f}] EV_ABS       ABS_MT_SLOT          00000001
[   {1.0 + end:.6f}] EV_ABS       ABS_MT_TRACKING_ID   ffffffff
[   {1.0 + end:.6f}] EV_ABS       ABS_MT_SLOT          00000000
[   {1.0 + end:.6f}] EV_ABS       ABS_MT_TRACKING_ID   ffffffff
[   {1.0 + end:.6f}] EV_SYN       SYN_REPORT           00000000
"""


def test_parallel_dual_hold_is_contact_group() -> None:
    profile = _profile()
    trace = _two_finger_trace(
        x1_start=0x100,
        y1_start=0x200,
        x1_end=0x110,
        y1_end=0x210,
        x2_start=0x300,
        y2_start=0x600,
        x2_end=0x310,
        y2_end=0x610,
    )
    actions = parse_getevent_trace(trace, profile)
    assert len(actions) == 1
    assert isinstance(actions[0], ContactGroupAction)
    assert actions[0].finger_count == 2


def test_span_changing_gesture_is_still_contacts() -> None:
    """Camera pinch is not special-cased — CoC interprets the MotionEvents."""
    profile = _profile()
    trace = _two_finger_trace(
        x1_start=0x100,
        y1_start=0x100,
        x1_end=0x400,
        y1_end=0x400,
        x2_start=0x500,
        y2_start=0x700,
        x2_end=0x200,
        y2_end=0x300,
    )
    actions = parse_getevent_trace(trace, profile)
    assert len(actions) == 1
    assert isinstance(actions[0], ContactGroupAction)


def test_five_finger_two_hand_chord_is_kept() -> None:
    """Two-handed deploy chords are intentional — keep all fingers (≤10)."""
    profile = _profile()
    lines = [
        "[   1.000000] EV_ABS       ABS_MT_SLOT          00000000",
        "[   1.000000] EV_ABS       ABS_MT_TRACKING_ID   00000001",
        "[   1.000000] EV_ABS       ABS_MT_POSITION_X    00000100",
        "[   1.000000] EV_ABS       ABS_MT_POSITION_Y    00000200",
        "[   1.000000] EV_SYN       SYN_REPORT           00000000",
    ]
    for slot, tid, x, y in (
        (1, 2, 0x180, 0x280),
        (2, 3, 0x200, 0x300),
        (3, 4, 0x280, 0x380),
        (4, 5, 0x300, 0x400),
    ):
        lines.extend(
            [
                f"[   1.020000] EV_ABS       ABS_MT_SLOT          {slot:08x}",
                f"[   1.020000] EV_ABS       ABS_MT_TRACKING_ID   {tid:08x}",
                f"[   1.020000] EV_ABS       ABS_MT_POSITION_X    {x:08x}",
                f"[   1.020000] EV_ABS       ABS_MT_POSITION_Y    {y:08x}",
                "[   1.020000] EV_SYN       SYN_REPORT           00000000",
            ]
        )
    # Hold all five, then release.
    for slot in (4, 3, 2, 1, 0):
        lines.extend(
            [
                f"[   2.500000] EV_ABS       ABS_MT_SLOT          {slot:08x}",
                "[   2.500000] EV_ABS       ABS_MT_TRACKING_ID   ffffffff",
            ]
        )
    lines.append("[   2.500000] EV_SYN       SYN_REPORT           00000000")
    actions = parse_getevent_trace("\n".join(lines), profile)
    assert len(actions) == 1
    assert isinstance(actions[0], ContactGroupAction)
    assert actions[0].finger_count == 5
