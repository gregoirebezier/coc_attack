import random

from coc_farm2.models import (
    AppBounds,
    ContactGroupAction,
    ContactSample,
    DeviceProfile,
    Macro,
    VariationConfig,
)
from coc_farm2.variation import pick_attack_template, vary_macro


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


def _group(delay_ms: int, x: int, y: int) -> ContactGroupAction:
    return ContactGroupAction(
        delay_ms=delay_ms,
        samples=(
            ContactSample(0, 0, x, y, "down"),
            ContactSample(40, 0, x, y, "up"),
        ),
    )


def test_variation_stays_in_bounds() -> None:
    macro = Macro(
        name="t",
        profile=_profile(),
        actions=(_group(10, 50, 50),),
    )
    rng = random.Random(0)
    varied = vary_macro(
        macro,
        VariationConfig(coord_sigma_px=20, delay_sigma_ms=5),
        rng=rng,
    )
    action = varied.actions[0]
    assert isinstance(action, ContactGroupAction)
    for sample in action.samples:
        assert macro.profile.app_bounds.contains(sample.x, sample.y)
    # Hold timing unchanged.
    assert action.duration_ms == 40


def test_pick_template() -> None:
    a = Macro(name="a", profile=_profile(), actions=(_group(0, 1, 1),))
    b = Macro(name="b", profile=_profile(), actions=(_group(0, 2, 2),))
    rng = random.Random(1)
    picks = {pick_attack_template([a, b], rng=rng).name for _ in range(20)}
    assert picks == {"a", "b"}
