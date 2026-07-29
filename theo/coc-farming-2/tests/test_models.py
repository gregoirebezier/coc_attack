import pytest

from coc_farm2.models import (
    AppBounds,
    ContactGroupAction,
    ContactSample,
    FarmConfig,
    LootThresholds,
    Point,
    action_from_dict,
)


def test_app_bounds_contains_and_clamp() -> None:
    bounds = AppBounds(10, 20, 100, 200)
    assert bounds.contains(10, 20)
    assert not bounds.contains(100, 20)
    assert bounds.clamp(0, 0) == (10, 20)
    assert bounds.clamp(500, 500) == (99, 199)


def test_contacts_roundtrip() -> None:
    action = ContactGroupAction(
        delay_ms=100,
        samples=(
            ContactSample(0, 0, 1, 2, "down"),
            ContactSample(50, 0, 1, 2, "up"),
        ),
    )
    restored = action_from_dict(action.to_dict())
    assert restored == action


def test_farm_config_defaults_roundtrip() -> None:
    config = FarmConfig(
        thresholds=LootThresholds(gold=1, elixir=2, dark=3),
        loot_mode="sum",
        sum_threshold=10,
        next_button=Point(10, 20),
        finish_battle_tap=Point(30, 40),
        finish_battle_confirm_tap=Point(50, 60),
        finish_loot_ratio=0.1,
    )
    restored = FarmConfig.from_dict(config.to_dict())
    assert restored.thresholds.gold == 1
    assert restored.loot_mode == "sum"
    assert restored.sum_threshold == 10
    assert restored.next_button == Point(10, 20)
    assert restored.finish_battle_tap == Point(30, 40)
    assert restored.finish_battle_confirm_tap == Point(50, 60)
    assert restored.finish_loot_ratio == 0.1


def test_sum_mode_requires_a_dedicated_positive_threshold() -> None:
    with pytest.raises(ValueError, match="sum_threshold must be positive"):
        FarmConfig(loot_mode="sum")
