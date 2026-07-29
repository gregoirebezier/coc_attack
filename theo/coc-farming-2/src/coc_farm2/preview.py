"""Render numbered contact-group overlays for macro inspection."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from coc_farm2.models import ContactGroupAction, Macro

_FINGER_COLORS = (
    (80, 220, 120),
    (80, 160, 255),
    (255, 180, 60),
    (220, 100, 220),
)


def render_macro_preview(
    macro: Macro,
    background: Image.Image | None = None,
    *,
    output_path: Path | None = None,
) -> Image.Image:
    if background is None:
        image = Image.new(
            "RGB",
            (macro.profile.logical_width, macro.profile.logical_height),
            color=(30, 32, 36),
        )
    else:
        image = background.convert("RGB").copy()
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    index = 0
    for action in macro.actions:
        if not isinstance(action, ContactGroupAction):
            continue
        index += 1
        for path in action.finger_paths():
            finger_id = 0
            # Recover finger id from first matching sample.
            if path:
                for sample in action.samples:
                    if sample.x == path[0][0] and sample.y == path[0][1]:
                        finger_id = sample.finger_id
                        break
            color = _FINGER_COLORS[finger_id % len(_FINGER_COLORS)]
            if len(path) >= 2:
                flat: list[float] = []
                for x, y, _t in path:
                    flat.extend((x, y))
                draw.line(flat, fill=color, width=2)
            _draw_point(draw, path[0][0], path[0][1], index, font, color=color)
            end = path[-1]
            draw.ellipse(
                (end[0] - 4, end[1] - 4, end[0] + 4, end[1] + 4),
                outline=color,
                width=2,
            )
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path)
    return image


def _draw_point(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    index: int,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    *,
    color: tuple[int, int, int],
) -> None:
    r = 10
    draw.ellipse((x - r, y - r, x + r, y + r), outline=color, width=2)
    draw.text((x + r + 2, y - r), str(index), fill=color, font=font)
