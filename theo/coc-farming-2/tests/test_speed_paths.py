"""Regression tests for farm-loop speed fixes."""

from __future__ import annotations

import random
import struct

from coc_farm2.adb import _parse_raw_screencap
from coc_farm2.models import (
    AppBounds,
    ContactGroupAction,
    ContactSample,
    DeviceProfile,
    Macro,
    VariationConfig,
)
from coc_farm2.ocr import parse_loot_number
from coc_farm2.variation import vary_macro


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


def test_zero_delay_stays_zero_under_jitter() -> None:
    macro = Macro(
        name="t",
        profile=_profile(),
        actions=(
            _group(0, 1, 1),
            _group(0, 2, 2),
            _group(0, 3, 3),
        ),
    )
    varied = vary_macro(
        macro,
        VariationConfig(coord_sigma_px=0, delay_sigma_ms=30),
        rng=random.Random(0),
    )
    assert all(action.delay_ms == 0 for action in varied.actions)


def test_parse_raw_screencap_rgba() -> None:
    width, height = 4, 2
    header = struct.pack("<IIII", width, height, 1, 0)
    pixels = bytes([255, 0, 0, 255] * (width * height))
    image = _parse_raw_screencap(header + pixels)
    assert image is not None
    assert image.size == (width, height)
    assert image.mode == "RGB"


def test_parse_loot_rejects_overlong_digit_runs() -> None:
    assert parse_loot_number("12345678") is None
    assert parse_loot_number("1234567") == 1234567
