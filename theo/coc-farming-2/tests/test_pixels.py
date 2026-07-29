from PIL import Image

from coc_farm2.models import PixelProbe
from coc_farm2.pixels import probe_matches_image, sample_median_rgb


def test_probe_matches_solid_color() -> None:
    image = Image.new("RGB", (50, 50), color=(200, 40, 40))
    probe = PixelProbe(
        name="t",
        x=10,
        y=10,
        radius=1,
        reference_rgb=(200, 40, 40),
        tolerance=5,
    )
    assert probe_matches_image(probe, image)
    assert sample_median_rgb(image, 10, 10, 1) == (200, 40, 40)
