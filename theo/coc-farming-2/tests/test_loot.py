from coc_farm2.loot import meets_thresholds
from coc_farm2.models import LootReading, LootThresholds


def test_meets_all_thresholds() -> None:
    thresholds = LootThresholds(gold=100, elixir=100, dark=0)
    assert meets_thresholds(
        LootReading(gold=100, elixir=200, dark=None),
        thresholds,
    )
    assert not meets_thresholds(
        LootReading(gold=99, elixir=200, dark=None),
        thresholds,
    )


def test_missing_ocr_fails() -> None:
    thresholds = LootThresholds(gold=100, elixir=0, dark=0)
    assert not meets_thresholds(LootReading(gold=None, elixir=50), thresholds)


def test_meets_sum_threshold_with_complete_or_partial_lower_bound() -> None:
    # Individual thresholds are deliberately unrelated to the dedicated sum.
    thresholds = LootThresholds(gold=5_000_000, elixir=5_000_000, dark=0)

    assert meets_thresholds(
        LootReading(gold=900_000, elixir=1_100_000),
        thresholds,
        mode="sum",
        sum_threshold=2_000_000,
    )
    assert meets_thresholds(
        LootReading(gold=None, elixir=2_020_265),
        thresholds,
        mode="sum",
        sum_threshold=2_000_000,
    )
    assert not meets_thresholds(
        LootReading(gold=None, elixir=1_999_999),
        thresholds,
        mode="sum",
        sum_threshold=2_000_000,
    )
