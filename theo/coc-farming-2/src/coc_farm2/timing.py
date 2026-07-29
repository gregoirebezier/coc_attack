"""Apply inter-group gap pacing for contact timelines (never reshape holds)."""

from __future__ import annotations

from dataclasses import replace

from coc_farm2.models import (
    ContactGroupAction,
    Macro,
    MacroAction,
    TimingConfig,
    WaitAction,
    WaitPixelAction,
    WaitPixelsAction,
)


def apply_timing(
    macro: Macro,
    config: TimingConfig,
    *,
    min_gap_ms: int | None = None,
) -> Macro:
    """
    Return a macro with optional inter-group gap scaling.

    Contact sample times are never modified. ``min_gap_ms`` floors delay before
    every action except the first (used for start_search menu animations).
    """
    actions = tuple(_scale_action(action, config) for action in macro.actions)
    if min_gap_ms is not None and min_gap_ms > 0 and actions:
        padded: list[MacroAction] = [actions[0]]
        for action in actions[1:]:
            padded.append(_with_delay(action, max(action.delay_ms, min_gap_ms)))
        actions = tuple(padded)
    return Macro(
        name=macro.name,
        profile=macro.profile,
        actions=actions,
        approved=macro.approved,
        source_take_name=macro.source_take_name,
    )


def _scale_action(action: MacroAction, config: TimingConfig) -> MacroAction:
    delay = _scale_delay(action.delay_ms, config)
    if isinstance(action, ContactGroupAction):
        return ContactGroupAction(delay_ms=delay, samples=action.samples)
    if isinstance(action, WaitAction):
        return WaitAction(delay_ms=delay, duration_ms=action.duration_ms)
    if isinstance(action, WaitPixelAction):
        return WaitPixelAction(
            delay_ms=delay,
            probe_name=action.probe_name,
            timeout_ms=action.timeout_ms,
        )
    if isinstance(action, WaitPixelsAction):
        return WaitPixelsAction(
            delay_ms=delay,
            probe_names=action.probe_names,
            timeout_ms=action.timeout_ms,
        )
    return action


def _scale_delay(delay_ms: int, config: TimingConfig) -> int:
    # delay_scale=0 used to strip gaps and break hold-based attacks.
    # Treat 0 as "keep recorded gaps" for contact timelines.
    if delay_ms <= 0:
        return 0
    if config.delay_scale <= 0:
        return delay_ms
    scaled = round(delay_ms * config.delay_scale)
    return max(config.min_delay_ms, scaled)


def _with_delay(action: MacroAction, delay_ms: int) -> MacroAction:
    if isinstance(
        action,
        (ContactGroupAction, WaitAction, WaitPixelAction, WaitPixelsAction),
    ):
        return replace(action, delay_ms=delay_ms)
    return action
