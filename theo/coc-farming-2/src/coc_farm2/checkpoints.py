"""Insert visual waits for long inter-group delays."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from coc_farm2.models import (
    ContactGroupAction,
    Macro,
    MacroAction,
    WaitAction,
    WaitPixelAction,
    WaitPixelsAction,
)

POST_CHECKPOINT_DELAY_MS = 250
MIN_CHECKPOINT_TIMEOUT_MS = 15_000
CHECKPOINT_TIMEOUT_PAD_MS = 10_000


class CheckpointError(ValueError):
    """Checkpoint cannot be applied to the macro."""


def gesture_delay_ms(action: MacroAction) -> int:
    return action.delay_ms


def unguarded_long_gestures(
    actions: Sequence[MacroAction],
    *,
    threshold_ms: int = 3_000,
) -> list[int]:
    """
    Return 1-based gesture indices whose delay exceeds threshold and is not
    preceded by a wait_pixel / wait_pixels action that already guards it.
    """
    result: list[int] = []
    for index, action in enumerate(actions):
        if not _is_gesture(action):
            continue
        if action.delay_ms < threshold_ms:
            continue
        if index > 0 and isinstance(
            actions[index - 1], (WaitPixelAction, WaitPixelsAction)
        ):
            continue
        result.append(index + 1)
    return result


def insert_checkpoint(
    macro: Macro,
    *,
    before_gesture: int,
    probe_name: str,
    timeout_ms: int | None = None,
) -> Macro:
    """Insert a wait_pixel immediately before the 1-based gesture index."""
    if before_gesture < 1 or before_gesture > len(macro.actions):
        raise CheckpointError(
            f"gesture index {before_gesture} is out of range (1..{len(macro.actions)})"
        )
    index = before_gesture - 1
    target = macro.actions[index]
    if not _is_gesture(target):
        raise CheckpointError(
            f"index {before_gesture} is not a gesture action ({target.kind})"
        )
    if index and isinstance(
        macro.actions[index - 1], (WaitPixelAction, WaitPixelsAction)
    ):
        raise CheckpointError(f"gesture {before_gesture} already has a checkpoint")

    derived_timeout = timeout_ms or max(
        MIN_CHECKPOINT_TIMEOUT_MS,
        target.delay_ms + CHECKPOINT_TIMEOUT_PAD_MS,
    )
    wait = WaitPixelAction(
        delay_ms=0,
        probe_name=probe_name,
        timeout_ms=derived_timeout,
    )
    shortened = replace(target, delay_ms=POST_CHECKPOINT_DELAY_MS)
    new_actions = (
        *macro.actions[:index],
        wait,
        shortened,
        *macro.actions[index + 1 :],
    )
    return Macro(
        name=macro.name,
        profile=macro.profile,
        actions=new_actions,
        approved=False,
        source_take_name=macro.source_take_name,
    )


def _is_gesture(action: MacroAction) -> bool:
    return isinstance(action, (ContactGroupAction, WaitAction))
