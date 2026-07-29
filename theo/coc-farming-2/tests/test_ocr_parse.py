from PIL import Image, ImageDraw

from coc_farm2.models import OcrRegion, Rect
from coc_farm2.ocr import (
    CallableOcrBackend,
    parse_loot_number,
    preprocess_loot_crop,
    read_loot,
)


def test_parse_loot_number_variants() -> None:
    assert parse_loot_number("412,345") == 412345
    assert parse_loot_number("412.345") == 412345
    assert parse_loot_number("  99 000 ") == 99000
    assert parse_loot_number("abc") is None
    assert parse_loot_number("7 768 390") is None


def test_preprocess_scales() -> None:
    image = Image.new("RGB", (10, 5), color=(10, 10, 10))
    out = preprocess_loot_crop(image, scale=2.0)
    assert out.size == (20, 10)


def test_read_loot_rejects_ocr_digit_insertions() -> None:
    image = Image.new("RGB", (165, 35), color=(20, 40, 20))
    draw = ImageDraw.Draw(image)
    for left in (10, 22, 34, 56, 68, 80):
        draw.rectangle((left, 5, left + 8, 29), fill=(245, 245, 245))

    reading = read_loot(
        image,
        {"gold": OcrRegion("gold", Rect(0, 0, 165, 35))},
        CallableOcrBackend(lambda _image: "3204595"),
        invert_retry=False,
    )

    assert reading.gold is None
