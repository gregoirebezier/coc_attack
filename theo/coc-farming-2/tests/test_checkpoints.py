from coc_farm2.checkpoints import insert_checkpoint, unguarded_long_gestures
from coc_farm2.models import (
    AppBounds,
    ContactGroupAction,
    ContactSample,
    DeviceProfile,
    Macro,
    WaitPixelAction,
)


def _group(delay_ms: int, x: int, y: int) -> ContactGroupAction:
    return ContactGroupAction(
        delay_ms=delay_ms,
        samples=(
            ContactSample(0, 0, x, y, "down"),
            ContactSample(20, 0, x, y, "up"),
        ),
    )


def _macro() -> Macro:
    profile = DeviceProfile(
        serial="X",
        model="M",
        android_api=34,
        package="p",
        activity="a",
        app_version="1",
        logical_width=10,
        logical_height=10,
        raw_width=10,
        raw_height=10,
        rotation=0,
        touch_device="/dev/input/event0",
        app_bounds=AppBounds(0, 0, 10, 10),
    )
    return Macro(
        name="s",
        profile=profile,
        actions=(
            _group(0, 1, 1),
            _group(5000, 2, 2),
        ),
    )


def test_unguarded_long_gestures() -> None:
    assert unguarded_long_gestures(_macro().actions) == [2]


def test_insert_checkpoint() -> None:
    macro = _macro()
    updated = insert_checkpoint(macro, before_gesture=2, probe_name="home")
    assert isinstance(updated.actions[1], WaitPixelAction)
    assert updated.actions[1].probe_name == "home"
    assert unguarded_long_gestures(updated.actions) == []
