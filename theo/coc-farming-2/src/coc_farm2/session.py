"""Flatten contact groups into one absolute-time session for single-JVM replay."""

from __future__ import annotations

from collections.abc import Sequence

from coc_farm2.models import ContactGroupAction


def flatten_contact_session(
    groups: Sequence[ContactGroupAction],
) -> tuple[tuple[int, int, int, int, str], ...]:
    """
    Build absolute session events: (t_ms, finger_id, x, y, phase).

    Inter-group ``delay_ms`` becomes empty time on the absolute clock (slept
    on-device inside one GestureInjector session — no per-group JVM restart).
    """
    if not groups:
        return ()
    events: list[tuple[int, int, int, int, str]] = []
    cursor_ms = 0
    for group in groups:
        cursor_ms += max(0, group.delay_ms)
        origin = cursor_ms
        for sample in group.samples:
            events.append(
                (
                    origin + sample.t_ms,
                    sample.finger_id,
                    sample.x,
                    sample.y,
                    sample.phase,
                )
            )
        cursor_ms = origin + group.duration_ms
    events.sort(key=lambda item: (item[0], item[1], _phase_rank(item[4])))
    return tuple(events)


def session_duration_ms(groups: Sequence[ContactGroupAction]) -> int:
    events = flatten_contact_session(groups)
    if not events:
        return 0
    return events[-1][0]


def _phase_rank(phase: str) -> int:
    if phase == "down":
        return 0
    if phase == "move":
        return 1
    return 2


def format_session_file(events: Sequence[tuple[int, int, int, int, str]]) -> str:
    lines = [f"{t} {finger} {x} {y} {phase}" for t, finger, x, y, phase in events]
    return "\n".join(lines) + ("\n" if lines else "")
