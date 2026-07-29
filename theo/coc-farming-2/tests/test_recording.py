from collections import Counter
from pathlib import Path

from coc_farm2.models import AppBounds, ContactGroupAction, DeviceProfile
from coc_farm2.recording import parse_getevent_trace, transform_raw_coordinate


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


def test_transform_rotation_1() -> None:
    profile = _profile()
    x, y = transform_raw_coordinate(0, 0, profile)
    assert x == 0
    assert y == profile.logical_height - 1


def test_parse_simple_tap_with_syn_report() -> None:
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


def test_incomplete_touch_is_skipped() -> None:
    profile = _profile()
    trace = """
[   1.000000] EV_ABS       ABS_MT_TRACKING_ID   00000001
[   1.001000] EV_ABS       ABS_MT_TRACKING_ID   ffffffff
[   2.000000] EV_ABS       ABS_MT_TRACKING_ID   00000002
[   2.000000] EV_ABS       ABS_MT_POSITION_X    00000064
[   2.000000] EV_ABS       ABS_MT_POSITION_Y    000000c8
[   2.000000] EV_SYN       SYN_REPORT           00000000
[   2.050000] EV_ABS       ABS_MT_TRACKING_ID   ffffffff
"""
    actions = parse_getevent_trace(trace, profile)
    assert len(actions) == 1
    assert isinstance(actions[0], ContactGroupAction)


def test_swipe_keeps_path_samples() -> None:
    profile = _profile()
    lines = [
        "[   1.000000] EV_ABS       ABS_MT_TRACKING_ID   00000001",
        "[   1.000000] EV_ABS       ABS_MT_POSITION_X    00000100",
        "[   1.000000] EV_ABS       ABS_MT_POSITION_Y    00000200",
        "[   1.000000] EV_SYN       SYN_REPORT           00000000",
    ]
    for i, raw_x in enumerate((0x120, 0x140, 0x180, 0x1C0, 0x200), start=1):
        t = 1.0 + i * 0.02
        lines.append(f"[   {t:.6f}] EV_ABS       ABS_MT_POSITION_X    {raw_x:08x}")
        lines.append(f"[   {t:.6f}] EV_SYN       SYN_REPORT           00000000")
    lines.append("[   1.200000] EV_ABS       ABS_MT_TRACKING_ID   ffffffff")
    actions = parse_getevent_trace("\n".join(lines), profile)
    assert len(actions) == 1
    assert isinstance(actions[0], ContactGroupAction)
    moves = [s for s in actions[0].samples if s.phase == "move"]
    assert len(moves) >= 3


def test_stationary_hold_keeps_full_duration() -> None:
    """Many identical frames must not delay finger-down / shorten the hold."""
    profile = _profile()
    lines = [
        "[   1.000000] EV_ABS       ABS_MT_TRACKING_ID   00000001",
        "[   1.000000] EV_ABS       ABS_MT_POSITION_X    00000100",
        "[   1.000000] EV_ABS       ABS_MT_POSITION_Y    00000200",
        "[   1.000000] EV_SYN       SYN_REPORT           00000000",
    ]
    for i in range(1, 50):
        t = 1.0 + i * 0.02
        lines.append(f"[   {t:.6f}] EV_SYN       SYN_REPORT           00000000")
    lines.extend(
        [
            "[   2.000000] EV_ABS       ABS_MT_TRACKING_ID   ffffffff",
            "[   2.000000] EV_SYN       SYN_REPORT           00000000",
        ]
    )
    actions = parse_getevent_trace("\n".join(lines), profile)
    assert len(actions) == 1
    assert isinstance(actions[0], ContactGroupAction)
    assert actions[0].duration_ms == 1000
    downs = [s for s in actions[0].samples if s.phase == "down"]
    ups = [s for s in actions[0].samples if s.phase == "up"]
    assert downs[0].t_ms == 0
    assert ups[0].t_ms == 1000


def test_real_attack_trace_compiles_to_contacts() -> None:
    root = Path(__file__).resolve().parents[1] / ".coc-farm2"
    trace_path = root / "takes" / "attack" / "01.getevent.txt"
    device_path = root / "device.json"
    if not trace_path.is_file() or not device_path.is_file():
        return
    import json

    profile = DeviceProfile.from_dict(json.loads(device_path.read_text()))
    actions = parse_getevent_trace(trace_path.read_text(), profile)
    counts = Counter(a.kind for a in actions)
    assert counts.get("contacts", 0) > 0
