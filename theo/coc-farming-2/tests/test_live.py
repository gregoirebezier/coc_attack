from PIL import Image

from coc_farm2.live import LiveProbeReader
from coc_farm2.models import PixelProbe


def test_matches_many_in_reuses_the_provided_frame() -> None:
    captures = 0

    def screenshot() -> Image.Image:
        nonlocal captures
        captures += 1
        return Image.new("RGB", (10, 10), color=(0, 0, 0))

    reader = LiveProbeReader(screenshot)
    frame = Image.new("RGB", (10, 10), color=(20, 30, 40))
    probes = {
        "ready": PixelProbe(
            name="ready",
            x=5,
            y=5,
            radius=0,
            reference_rgb=(20, 30, 40),
            tolerance=0,
        ),
        "other": PixelProbe(
            name="other",
            x=5,
            y=5,
            radius=0,
            reference_rgb=(1, 2, 3),
            tolerance=0,
        ),
    }

    assert reader.matches_many_in(frame, probes) == {
        "ready": True,
        "other": False,
    }
    assert captures == 0
