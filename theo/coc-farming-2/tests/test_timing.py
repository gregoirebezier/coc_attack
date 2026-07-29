from coc_farm2.models import (
    AppBounds,
    ContactGroupAction,
    ContactSample,
    DeviceProfile,
    Macro,
    TimingConfig,
)
from coc_farm2.timing import apply_timing


def _profile() -> DeviceProfile:
    return DeviceProfile(
        serial="X",
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


def _group(delay_ms: int, x: int, y: int, hold_ms: int = 50) -> ContactGroupAction:
    return ContactGroupAction(
        delay_ms=delay_ms,
        samples=(
            ContactSample(0, 0, x, y, "down"),
            ContactSample(hold_ms, 0, x, y, "up"),
        ),
    )


def test_apply_timing_scales_inter_group_gaps_only() -> None:
    macro = Macro(
        name="t",
        profile=_profile(),
        actions=(
            _group(0, 1, 1, hold_ms=2000),
            _group(400, 2, 2, hold_ms=80),
        ),
    )
    out = apply_timing(macro, TimingConfig(delay_scale=0.5, min_delay_ms=0))
    assert isinstance(out.actions[0], ContactGroupAction)
    assert out.actions[0].duration_ms == 2000  # hold untouched
    assert out.actions[1].delay_ms == 200  # 400 * 0.5


def test_delay_scale_zero_keeps_recorded_gaps() -> None:
    macro = Macro(
        name="t",
        profile=_profile(),
        actions=(
            _group(0, 1, 1),
            _group(400, 2, 2),
            _group(1000, 3, 3),
        ),
    )
    out = apply_timing(macro, TimingConfig(delay_scale=0.0, min_delay_ms=0))
    assert out.actions[1].delay_ms == 400
    assert out.actions[2].delay_ms == 1000


def test_start_search_min_gap() -> None:
    macro = Macro(
        name="start_search",
        profile=_profile(),
        actions=(
            _group(0, 1, 1),
            _group(0, 2, 2),
            _group(200, 3, 3),
        ),
    )
    out = apply_timing(
        macro,
        TimingConfig(delay_scale=1.0, min_delay_ms=0),
        min_gap_ms=1000,
    )
    assert out.actions[0].delay_ms == 0
    assert out.actions[1].delay_ms == 1000
    assert out.actions[2].delay_ms == 1000
