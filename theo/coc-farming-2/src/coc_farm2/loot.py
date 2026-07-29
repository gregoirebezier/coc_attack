"""Loot threshold comparison helpers."""

from __future__ import annotations

from coc_farm2.models import LootReading, LootThresholds


def meets_thresholds(
    reading: LootReading,
    thresholds: LootThresholds,
    *,
    mode: str = "all",
    sum_threshold: int = 0,
) -> bool:
    """Return True when the OCR reading satisfies configured thresholds."""
    if mode == "sum":
        if sum_threshold <= 0:
            raise ValueError("sum_threshold must be positive in sum mode")
        known_total = (reading.gold or 0) + (reading.elixir or 0)
        return known_total >= sum_threshold

    if mode != "all":
        raise ValueError(f"unsupported loot mode: {mode!r}")

    checks: list[bool] = []
    if thresholds.gold > 0:
        if reading.gold is None:
            return False
        checks.append(reading.gold >= thresholds.gold)
    if thresholds.elixir > 0:
        if reading.elixir is None:
            return False
        checks.append(reading.elixir >= thresholds.elixir)
    if thresholds.dark > 0:
        if reading.dark is None:
            return False
        checks.append(reading.dark >= thresholds.dark)

    # If every threshold is zero, accept any readable base.
    if not checks:
        return reading.gold is not None or reading.elixir is not None
    return all(checks)


def format_loot(reading: LootReading) -> str:
    parts = [
        f"gold={_fmt(reading.gold)}",
        f"elixir={_fmt(reading.elixir)}",
        f"dark={_fmt(reading.dark)}",
    ]
    return " ".join(parts)


def _fmt(value: int | None) -> str:
    return "?" if value is None else f"{value:,}"
