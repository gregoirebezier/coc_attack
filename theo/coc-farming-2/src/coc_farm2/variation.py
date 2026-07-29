"""Gaussian sampling for coordinate and inter-group gap jitter."""

from __future__ import annotations

import random
from collections.abc import Sequence

from coc_farm2.models import (
    AppBounds,
    ContactGroupAction,
    ContactSample,
    Macro,
    MacroAction,
    VariationConfig,
    WaitAction,
    WaitPixelAction,
    WaitPixelsAction,
)


def vary_macro(
    macro: Macro,
    config: VariationConfig,
    *,
    rng: random.Random | None = None,
) -> Macro:
    """Jitter sample positions and inter-group gaps; never reshape hold times."""
    engine = rng or random.Random()
    varied = tuple(
        vary_action(action, config, bounds=macro.profile.app_bounds, rng=engine)
        for action in macro.actions
    )
    return Macro(
        name=macro.name,
        profile=macro.profile,
        actions=varied,
        approved=macro.approved,
        source_take_name=macro.source_take_name,
    )


def vary_action(
    action: MacroAction,
    config: VariationConfig,
    *,
    bounds: AppBounds,
    rng: random.Random,
) -> MacroAction:
    if isinstance(action, ContactGroupAction):
        jittered: list[ContactSample] = []
        for sample in action.samples:
            x, y = _jitter_point(sample.x, sample.y, config.coord_sigma_px, bounds, rng)
            jittered.append(
                ContactSample(
                    t_ms=sample.t_ms,
                    finger_id=sample.finger_id,
                    x=x,
                    y=y,
                    phase=sample.phase,
                )
            )
        return ContactGroupAction(
            delay_ms=_jitter_delay(action.delay_ms, config.delay_sigma_ms, rng),
            samples=tuple(jittered),
        )
    if isinstance(action, WaitAction):
        return WaitAction(
            delay_ms=_jitter_delay(action.delay_ms, config.delay_sigma_ms, rng),
            duration_ms=action.duration_ms,
        )
    if isinstance(action, WaitPixelAction):
        return WaitPixelAction(
            delay_ms=_jitter_delay(action.delay_ms, config.delay_sigma_ms, rng),
            probe_name=action.probe_name,
            timeout_ms=action.timeout_ms,
        )
    if isinstance(action, WaitPixelsAction):
        return WaitPixelsAction(
            delay_ms=_jitter_delay(action.delay_ms, config.delay_sigma_ms, rng),
            probe_names=action.probe_names,
            timeout_ms=action.timeout_ms,
        )
    return action


def pick_attack_template(
    templates: Sequence[Macro],
    *,
    rng: random.Random | None = None,
) -> Macro:
    if not templates:
        raise ValueError("no attack templates available")
    engine = rng or random.Random()
    return engine.choice(list(templates))


def _jitter_point(
    x: int,
    y: int,
    sigma: float,
    bounds: AppBounds,
    rng: random.Random,
) -> tuple[int, int]:
    if sigma <= 0:
        return bounds.clamp(x, y)
    jx = int(round(rng.gauss(x, sigma)))
    jy = int(round(rng.gauss(y, sigma)))
    return bounds.clamp(jx, jy)


def _jitter_delay(value: int, sigma: float, rng: random.Random) -> int:
    if value <= 0 or sigma <= 0:
        return max(0, value)
    return max(0, int(round(rng.gauss(value, sigma))))
