"""Pixel probe evaluation against screenshots."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from statistics import median

from PIL import Image

from coc_farm2.models import PixelProbe

RGB = tuple[int, int, int]


class ProbeEvaluator:
    """Track a rolling K-of-N window of consecutive screenshots."""

    def __init__(self, probe: PixelProbe) -> None:
        self.probe = probe
        self._matches: deque[bool] = deque(maxlen=probe.sample_count)

    def observe(self, image: Image.Image) -> bool:
        self._matches.append(probe_matches_image(self.probe, image))
        return (
            len(self._matches) == self.probe.sample_count
            and sum(self._matches) >= self.probe.required_matches
        )

    def reset(self) -> None:
        self._matches.clear()


def evaluate_probe_window(
    probe: PixelProbe,
    images: Iterable[Image.Image],
) -> bool:
    evaluator = ProbeEvaluator(probe)
    matched = False
    for image in images:
        matched = evaluator.observe(image)
    return matched


def evaluate_probe_group_window(
    probes: Iterable[PixelProbe],
    images: Iterable[Image.Image],
) -> bool:
    selected = tuple(probes)
    if len(selected) < 2:
        raise ValueError("a probe group requires at least two probes")
    sample_count = selected[0].sample_count
    required_matches = selected[0].required_matches
    if any(
        probe.sample_count != sample_count or probe.required_matches != required_matches
        for probe in selected[1:]
    ):
        raise ValueError(
            "grouped probes must use the same sample_count and required_matches"
        )

    window = tuple(images)[-sample_count:]
    if len(window) != sample_count:
        return False
    matches = sum(
        all(probe_matches_image(probe, image) for probe in selected) for image in window
    )
    return matches >= required_matches


def probe_matches_image(probe: PixelProbe, image: Image.Image) -> bool:
    rgb = sample_median_rgb(image, probe.x, probe.y, probe.radius)
    return rgb_within_tolerance(rgb, probe.reference_rgb, probe.tolerance)


def sample_median_rgb(
    image: Image.Image,
    x: int,
    y: int,
    radius: int,
) -> RGB:
    if image.mode != "RGB":
        image = image.convert("RGB")
    width, height = image.size
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError(f"sample point ({x}, {y}) is outside image {width}x{height}")
    left = max(0, x - radius)
    top = max(0, y - radius)
    right = min(width, x + radius + 1)
    bottom = min(height, y + radius + 1)
    region = image.crop((left, top, right, bottom))
    # Pillow 12 still exposes getdata; avoid deprecated API when available.
    get_flat = getattr(region, "get_flattened_data", None)
    if callable(get_flat):
        pixels = list(get_flat())
    else:
        pixels = list(region.getdata())
    if not pixels:
        raise ValueError("sample region is empty")
    reds = [pixel[0] for pixel in pixels]
    greens = [pixel[1] for pixel in pixels]
    blues = [pixel[2] for pixel in pixels]
    return (int(median(reds)), int(median(greens)), int(median(blues)))


def rgb_within_tolerance(
    observed: RGB,
    reference: RGB,
    tolerance: int,
) -> bool:
    return all(
        abs(obs - ref) <= tolerance
        for obs, ref in zip(observed, reference, strict=True)
    )


def build_stable_probe(
    name: str,
    x: int,
    y: int,
    samples: Sequence[RGB],
    *,
    radius: int = 2,
    tolerance: int = 24,
    required_matches: int = 2,
    sample_count: int = 3,
    max_channel_drift: int = 18,
) -> PixelProbe:
    """Build a probe from multi-frame samples, rejecting unstable colors."""
    if len(samples) < sample_count:
        raise ValueError(f"need at least {sample_count} RGB samples for probe {name!r}")
    channels = list(zip(*samples, strict=True))
    for channel_values in channels:
        if max(channel_values) - min(channel_values) > max_channel_drift:
            raise ValueError(
                f"probe {name!r} is unstable across samples "
                f"(channel drift > {max_channel_drift})"
            )
    reference = (
        int(median([s[0] for s in samples])),
        int(median([s[1] for s in samples])),
        int(median([s[2] for s in samples])),
    )
    return PixelProbe(
        name=name,
        x=x,
        y=y,
        radius=radius,
        reference_rgb=reference,
        tolerance=tolerance,
        required_matches=required_matches,
        sample_count=sample_count,
    )
