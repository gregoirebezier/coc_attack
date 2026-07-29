"""Live probe reading from a device screenshot source."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from PIL import Image

from coc_farm2.models import PixelProbe
from coc_farm2.pixels import (
    evaluate_probe_group_window,
    evaluate_probe_window,
    probe_matches_image,
)


class LiveProbeReader:
    """
    Screenshot-based probe matcher.

    Farming defaults to a single frame (``sample_count=1``) so home/resource
    checks take one screencap instead of 3×N with gaps.
    """

    def __init__(
        self,
        screenshot: Callable[[], Image.Image],
        *,
        inter_frame_sleeper: Callable[[float], None] | None = None,
        frame_gap_s: float = 0.0,
        sample_count: int | None = 1,
    ) -> None:
        self.screenshot = screenshot
        self.inter_frame_sleeper = inter_frame_sleeper or (lambda _s: None)
        self.frame_gap_s = frame_gap_s
        # None → use each probe's own sample_count (slow, multi-frame).
        self.sample_count = sample_count

    def invalidate(self) -> None:
        """Compatibility hook; live frames are deliberately never retained."""
        return None

    def matches(self, probe: PixelProbe) -> bool:
        if self.sample_count is not None:
            count = self.sample_count
        else:
            count = probe.sample_count
        images = self._capture_window(max(1, count))
        if count <= 1:
            return probe_matches_image(probe, images[0])
        return evaluate_probe_window(probe, images)

    def matches_group(self, probes: Sequence[PixelProbe]) -> bool:
        if not probes:
            raise ValueError("probe group is empty")
        count = (
            self.sample_count
            if self.sample_count is not None
            else probes[0].sample_count
        )
        images = self._capture_window(max(1, count))
        if count <= 1:
            return all(probe_matches_image(probe, images[0]) for probe in probes)
        return evaluate_probe_group_window(probes, images)

    def matches_many(
        self,
        probes: Mapping[str, PixelProbe],
    ) -> dict[str, bool]:
        """Evaluate many probes against one shared screenshot (fast preflight)."""
        return self.matches_many_in(self.screenshot(), probes)

    def matches_many_in(
        self,
        image: Image.Image,
        probes: Mapping[str, PixelProbe],
    ) -> dict[str, bool]:
        """Evaluate many probes against a caller-owned screenshot."""
        return {
            name: probe_matches_image(probe, image) for name, probe in probes.items()
        }

    def _capture_window(self, count: int) -> list[Image.Image]:
        frames: list[Image.Image] = []
        for index in range(count):
            frames.append(self.screenshot())
            if index + 1 < count and self.frame_gap_s > 0:
                self.inter_frame_sleeper(self.frame_gap_s)
        return frames
