"""OCR of CoC loot numbers from calibrated screen crops.

EasyOCR does **not** use Apple MPS (CUDA or CPU only), so on Mac it is slow.
For loot digits we prefer Tesseract (CPU, ~10–50ms/crop) and avoid lazy-loading
EasyOCR mid-cycle (multi-second stall).
"""

from __future__ import annotations

import re
import shutil
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol

from PIL import Image, ImageOps

from coc_farm2.models import LootReading, OcrRegion

_DIGITS = re.compile(r"[^\d]")
# Bright CoC loot glyphs (friend's bot): min(RGB) above this → digit ink.
_LOOT_BRIGHT_MIN = 160
_DEFAULT_LOOT_SCALE = 4.0
_MAX_PLAUSIBLE_LOOT = 5_000_000


class OcrBackend(Protocol):
    def read_text(self, image: Image.Image) -> str: ...

    def warmup(self) -> None: ...


class OcrError(RuntimeError):
    """Raised when OCR cannot be performed."""


@dataclass(slots=True)
class CallableOcrBackend:
    """Test double / custom backend."""

    fn: Callable[[Image.Image], str]

    def read_text(self, image: Image.Image) -> str:
        return self.fn(image)

    def warmup(self) -> None:
        return None


@dataclass(slots=True)
class TesseractOcrBackend:
    """Fast digit OCR via system Tesseract (preferred on macOS)."""

    psm: int = 7  # treat crop as a single text line
    _ready: bool = False

    def read_text(self, image: Image.Image) -> str:
        try:
            import pytesseract  # type: ignore[import-untyped]
        except ImportError as error:
            raise OcrError(
                "pytesseract is not installed. Run: uv sync --extra ocr"
            ) from error
        # Whitelist digits only — loot never has letters.
        config = (
            f"--oem 1 --psm {self.psm} "
            "-c tessedit_char_whitelist=0123456789 "
            "-c load_system_dawg=0 -c load_freq_dawg=0"
        )
        mono = image.convert("L")
        text = pytesseract.image_to_string(mono, config=config)
        return str(text)

    def warmup(self) -> None:
        if self._ready:
            return
        # Tiny blank image so tessdata is loaded once.
        blank = Image.new("L", (32, 16), color=255)
        _ = self.read_text(blank)
        self._ready = True


@dataclass(slots=True)
class EasyOcrBackend:
    """EasyOCR wrapper. Uses CUDA when available; MPS is not supported by EasyOCR."""

    prefer_gpu: bool = True
    _reader: object | None = None
    _device_note: str = "cpu"

    def read_text(self, image: Image.Image) -> str:
        reader = self._ensure_reader()
        import numpy as np

        array = np.array(image.convert("RGB"))
        # detail=0 returns strings only; paragraph=False is faster.
        results = reader.readtext(  # type: ignore[attr-defined]
            array,
            allowlist="0123456789",
            detail=0,
            paragraph=False,
        )
        if isinstance(results, list):
            return " ".join(str(item) for item in results)
        return str(results)

    def warmup(self) -> None:
        self._ensure_reader()
        blank = Image.new("RGB", (64, 24), color=(255, 255, 255))
        _ = self.read_text(blank)

    def _ensure_reader(self) -> object:
        if self._reader is not None:
            return self._reader
        try:
            import easyocr  # type: ignore[import-untyped]
            import torch
        except ImportError as error:
            raise OcrError(
                "EasyOCR is not installed. Run: uv sync --extra ocr"
            ) from error

        use_cuda = self.prefer_gpu and torch.cuda.is_available()
        # EasyOCR only honors CUDA via gpu=True; Apple MPS is not used.
        mps = bool(
            getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
        )
        if use_cuda:
            self._device_note = "cuda"
        elif mps:
            self._device_note = "cpu (MPS available but unused by EasyOCR)"
        else:
            self._device_note = "cpu"
        try:
            self._reader = easyocr.Reader(
                ["en"],
                gpu=use_cuda,
                verbose=False,
                quantize=not use_cuda,
            )
        except TypeError:
            # Older EasyOCR without quantize=
            self._reader = easyocr.Reader(["en"], gpu=use_cuda, verbose=False)
        return self._reader


@dataclass(slots=True)
class FallbackOcrBackend:
    """Try a fast backend first; lazy-load fallback only on parse miss."""

    primary: OcrBackend
    fallback_factory: Callable[[], OcrBackend]
    _fallback: OcrBackend | None = None
    _warm: bool = False

    def read_text(self, image: Image.Image) -> str:
        text = self.primary.read_text(image)
        if parse_loot_number(text) is not None:
            return text
        if self._fallback is None:
            self._fallback = self.fallback_factory()
        return self._fallback.read_text(image)

    def warmup(self) -> None:
        if self._warm:
            return
        # Only warm the fast path — EasyOCR load is multi-second on CPU.
        self.primary.warmup()
        self._warm = True


def create_ocr_backend(*, prefer: str = "auto") -> tuple[OcrBackend, str]:
    """
    Build the best available OCR backend.

    Returns ``(backend, description)``.
    Preference: tesseract (fast CPU digits) → easyocr only if tesseract missing
    or prefer=\"easyocr\" / \"fallback\".
    """
    prefer = prefer.lower().strip()
    tesseract_ok = _tesseract_available()
    easy_ok = _easyocr_available()

    if prefer == "tesseract":
        if not tesseract_ok:
            raise OcrError(
                "Tesseract not available. Install: brew install tesseract "
                "&& uv sync --extra ocr"
            )
        return TesseractOcrBackend(), "tesseract (CPU, digit whitelist)"

    if prefer == "easyocr":
        if not easy_ok:
            raise OcrError("EasyOCR not available. Run: uv sync --extra ocr")
        backend = EasyOcrBackend()
        backend._ensure_reader()
        return backend, f"easyocr ({backend._device_note})"

    if prefer == "fallback":
        # Explicit slow path: tesseract then lazy EasyOCR on miss.
        if tesseract_ok and easy_ok:
            return (
                FallbackOcrBackend(
                    primary=TesseractOcrBackend(),
                    fallback_factory=lambda: EasyOcrBackend(),
                ),
                "tesseract (fast) + lazy easyocr fallback",
            )
        if tesseract_ok:
            return TesseractOcrBackend(), "tesseract (CPU, digit whitelist)"
        if easy_ok:
            backend = EasyOcrBackend()
            backend._ensure_reader()
            return backend, f"easyocr ({backend._device_note})"
        raise OcrError("No OCR backend available for prefer=fallback")

    # auto — Tesseract only when available (never cold-load EasyOCR mid-farm).
    if tesseract_ok:
        return TesseractOcrBackend(), "tesseract (CPU, digit whitelist)"

    if easy_ok:
        backend = EasyOcrBackend()
        backend._ensure_reader()
        return backend, f"easyocr ({backend._device_note})"

    raise OcrError(
        "No OCR backend available. Install Tesseract (recommended on Mac):\n"
        "  brew install tesseract\n"
        "  uv sync --extra ocr\n"
        "Or EasyOCR only: uv sync --extra ocr"
    )


def _tesseract_available() -> bool:
    if shutil.which("tesseract") is None:
        return False
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return True


def _easyocr_available() -> bool:
    try:
        import easyocr  # noqa: F401
    except ImportError:
        return False
    return True


def preprocess_loot_crop(
    image: Image.Image,
    *,
    scale: float = _DEFAULT_LOOT_SCALE,
) -> Image.Image:
    """
    Isolate bright loot digits, invert, and upscale for Tesseract.

    Matches the coc_attack pipeline: min(RGB) mask → invert → LANCZOS upscale.
    """
    rgb = image.convert("RGB")
    pixels = rgb.load()
    mask = Image.new("L", rgb.size)
    mp = mask.load()
    assert pixels is not None and mp is not None
    width, height = rgb.size
    threshold = _LOOT_BRIGHT_MIN
    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y][:3]
            mp[x, y] = 255 if min(r, g, b) > threshold else 0
    # Digits become black on white (Tesseract-friendly).
    inverted = ImageOps.invert(mask)
    if scale != 1.0:
        out_w = max(1, int(round(inverted.width * scale)))
        out_h = max(1, int(round(inverted.height * scale)))
        inverted = inverted.resize((out_w, out_h), Image.Resampling.LANCZOS)
    return inverted.convert("RGB")


def count_visible_loot_digits(image: Image.Image) -> int:
    """Count stable bright glyph runs without trusting OCR output."""
    rgb = image.convert("RGB")
    pixels = rgb.load()
    assert pixels is not None
    min_width = max(2, rgb.height // 5)
    max_gap = max(3, int(rgb.height * 0.38))
    counts: list[int] = []

    for threshold in (150, 165, 180, 195):
        columns = [
            any(min(pixels[x, y][:3]) > threshold for y in range(rgb.height))
            for x in range(rgb.width)
        ]
        runs: list[tuple[int, int]] = []
        start: int | None = None
        for x, bright in enumerate([*columns, False]):
            if bright and start is None:
                start = x
            elif not bright and start is not None:
                if x - start >= min_width:
                    runs.append((start, x))
                start = None

        clusters: list[list[tuple[int, int]]] = []
        for run in runs:
            if not clusters or run[0] - clusters[-1][-1][1] > max_gap:
                clusters.append([run])
            else:
                clusters[-1].append(run)
        if clusters:
            counts.append(max(len(cluster) for cluster in clusters))

    if not counts:
        return 0
    frequencies = Counter(counts)
    digit_count, confirmations = max(
        frequencies.items(),
        key=lambda item: (item[1], item[0]),
    )
    # Only constrain OCR when at least two thresholds independently agree.
    return digit_count if confirmations >= 2 else 0


def _loot_digits(text: str) -> str:
    cleaned = text.strip().replace(" ", "").replace(".", "").replace(",", "")
    return _DIGITS.sub("", cleaned)


def parse_loot_number(text: str) -> int | None:
    """Parse CoC-style number strings into integers."""
    cleaned = _loot_digits(text)
    if not cleaned:
        return None
    # Loot rarely exceeds 7 digits; longer usually means OCR garbage.
    if len(cleaned) > 7:
        return None
    try:
        value = int(cleaned)
    except ValueError:
        return None
    if value > _MAX_PLAUSIBLE_LOOT:
        return None
    return value


def crop_region(image: Image.Image, region: OcrRegion) -> Image.Image:
    rect = region.rect
    if rect.right > image.width or rect.bottom > image.height:
        raise OcrError(
            f"OCR region {region.name!r} exceeds screenshot "
            f"{image.width}x{image.height}"
        )
    return image.crop((rect.left, rect.top, rect.right, rect.bottom))


def read_loot(
    image: Image.Image,
    regions: Mapping[str, OcrRegion],
    backend: OcrBackend,
    *,
    invert_retry: bool = True,
) -> LootReading:
    """Read gold/elixir/dark from named OCR regions (parallel when multiple)."""

    def read_named(name: str) -> int | None:
        region = regions.get(name)
        if region is None:
            return None
        crop = crop_region(image, region)
        expected_digits = count_visible_loot_digits(crop)
        # Legacy calibrated scale=2.0 meant "default"; use the fast/accurate 4×.
        scale = region.scale if region.scale not in {0.0, 2.0} else _DEFAULT_LOOT_SCALE
        prepared = preprocess_loot_crop(crop, scale=scale)

        def parse_checked(text: str) -> int | None:
            value = parse_loot_number(text)
            if (
                value is not None
                and expected_digits > 0
                and len(_loot_digits(text)) != expected_digits
            ):
                return None
            return value

        value = parse_checked(backend.read_text(prepared))
        if value is not None or not invert_retry:
            return value
        # Retry inverted crop for light-on-dark digits (one extra OCR only on miss).
        inverted = ImageOps.invert(prepared.convert("L")).convert("RGB")
        return parse_checked(backend.read_text(inverted))

    names = ("gold", "elixir", "dark")
    present = [name for name in names if name in regions]
    if len(present) <= 1:
        return LootReading(
            gold=read_named("gold"),
            elixir=read_named("elixir"),
            dark=read_named("dark"),
        )

    with ThreadPoolExecutor(max_workers=len(present)) as pool:
        futures = {name: pool.submit(read_named, name) for name in present}
        values = {name: futures[name].result() for name in present}
    return LootReading(
        gold=values.get("gold"),
        elixir=values.get("elixir"),
        dark=values.get("dark"),
    )


@dataclass
class WarmOcr:
    """Hold a warmed backend for the process lifetime."""

    backend: OcrBackend
    description: str
    _done: bool = field(default=False, init=False, repr=False)

    def ensure_warm(self) -> OcrBackend:
        if not self._done:
            self.backend.warmup()
            self._done = True
        return self.backend
