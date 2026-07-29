"""Parse multitouch getevent traces into lossless contact-group timelines."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from coc_farm2.models import (
    ContactGroupAction,
    ContactSample,
    DeviceProfile,
    MacroAction,
)

# Ghost contact: short + almost stationary while another finger does real work.
_GHOST_MAX_DURATION_S = 0.060
_GHOST_MAX_MOTION_PX = 36
# Two-handed deploy chords are real (not ghosts). Match Android's typical
# pointer ceiling; injector / ContactGroupAction share this limit.
_MAX_CONTACTS = 10


class RecordingError(ValueError):
    """The touchscreen trace cannot be converted into a safe macro."""


class MultitouchError(RecordingError):
    """The trace contains a contact group that cannot be replayed safely."""


@dataclass(slots=True)
class _Contact:
    tracking_id: int
    down_at: float
    ended_at: float | None = None
    raw_x: int | None = None
    raw_y: int | None = None
    # Frame samples: (timestamp, raw_x, raw_y) on each SYN_REPORT.
    samples: list[tuple[float, int, int]] = field(default_factory=list)

    def commit_sample(self, timestamp: float) -> None:
        if self.raw_x is None or self.raw_y is None:
            return
        sample = (timestamp, self.raw_x, self.raw_y)
        if self.samples and self.samples[-1][1:] == sample[1:]:
            # Keep first time for a stationary hold; don't spam identical points.
            return
        self.samples.append(sample)

    @property
    def duration_s(self) -> float:
        if self.ended_at is None:
            return 0.0
        return max(0.0, self.ended_at - self.down_at)


_EVENT_LINE = re.compile(
    r"^\[\s*(?P<timestamp>\d+(?:\.\d+)?)\]\s+"
    r"(?:(?P<device>\S+):\s+)?"
    r"(?P<event_type>\S+)\s+"
    r"(?P<event_code>\S+)\s+"
    r"(?P<value>\S+)\s*$"
)

_EVENT_CODES = {
    "ABS_MT_SLOT": "slot",
    "002f": "slot",
    "ABS_MT_TRACKING_ID": "tracking_id",
    "0039": "tracking_id",
    "ABS_MT_POSITION_X": "x",
    "0035": "x",
    "ABS_MT_POSITION_Y": "y",
    "0036": "y",
}


def parse_getevent_trace(
    trace: str,
    profile: DeviceProfile,
) -> tuple[MacroAction, ...]:
    """
    Convert a Samsung ``getevent -lt`` trace into contact-group actions.

    No tap/swipe/pinch classification, hold caps, or burst aggregation —
    CoC interprets the MotionEvent stream on replay.
    """
    current_slot = 0
    contacts: dict[int, _Contact] = {}
    gestures: list[tuple[_Contact, ...]] = []
    gesture_contacts: list[_Contact] = []
    previous_timestamp: float | None = None
    saw_touch_event = False
    skipped_incomplete = 0
    slot_last_x: dict[int, int] = {}
    slot_last_y: dict[int, int] = {}

    for line_number, line in enumerate(trace.splitlines(), start=1):
        match = _EVENT_LINE.match(line.strip())
        if match is None:
            continue
        device = match.group("device")
        if device is not None and device != profile.touch_device:
            continue

        event_type = match.group("event_type")
        event_code = match.group("event_code")
        event: str | None
        if event_type in {"EV_SYN", "0000"} and event_code in {
            "SYN_REPORT",
            "0000",
            "REPORT",
        }:
            event = "syn"
        else:
            event = _EVENT_CODES.get(event_code) or _EVENT_CODES.get(event_code.lower())
        if event is None:
            continue

        saw_touch_event = True
        timestamp = float(match.group("timestamp"))
        if previous_timestamp is not None and timestamp < previous_timestamp:
            raise RecordingError(
                f"touch timestamps go backwards at trace line {line_number}"
            )
        previous_timestamp = timestamp

        if event == "syn":
            for contact in contacts.values():
                contact.commit_sample(timestamp)
            continue

        value = _parse_hex_value(match.group("value"), line_number)
        if event == "slot":
            current_slot = value
            if current_slot < 0:
                raise RecordingError(
                    f"negative multitouch slot at trace line {line_number}"
                )
            continue

        if event == "tracking_id":
            if value == -1:
                released = contacts.pop(current_slot, None)
                if released is None:
                    continue
                _seed_contact_from_slot_cache(
                    released, current_slot, slot_last_x, slot_last_y
                )
                released.commit_sample(timestamp)
                if not released.samples:
                    _discard_contact(gesture_contacts, released)
                    skipped_incomplete += 1
                    continue
                released.ended_at = timestamp
                if not contacts:
                    complete = tuple(
                        item
                        for item in gesture_contacts
                        if item.samples and item.ended_at is not None
                    )
                    if complete:
                        gestures.append(_sanitize_gesture(complete, profile))
                    gesture_contacts = []
                continue

            if current_slot in contacts:
                raise RecordingError(
                    "touch slot was reused before release "
                    f"(slot {current_slot}, trace line {line_number})"
                )
            if any(c.tracking_id == value for c in contacts.values()):
                raise RecordingError(
                    "touch tracking ID was reused while still active "
                    f"(ID {value}, trace line {line_number})"
                )
            # Parse all concurrent contacts; sanitize trims only above
            # _MAX_CONTACTS (two-handed chords are kept intact).
            contact = _Contact(tracking_id=value, down_at=timestamp)
            _seed_contact_from_slot_cache(
                contact, current_slot, slot_last_x, slot_last_y
            )
            contacts[current_slot] = contact
            gesture_contacts.append(contact)
            continue

        active = contacts.get(current_slot)
        if active is None:
            if event == "x":
                slot_last_x[current_slot] = value
            else:
                slot_last_y[current_slot] = value
            continue
        if event == "x":
            active.raw_x = value
            slot_last_x[current_slot] = value
        else:
            active.raw_y = value
            slot_last_y[current_slot] = value

    if contacts:
        active_slots = ", ".join(str(slot) for slot in sorted(contacts))
        raise RecordingError(
            "touch trace ended before release for active slot(s) "
            f"{active_slots}; record the take again"
        )
    if not gestures:
        detail = (
            "touch events were present but no complete gesture was recorded"
            if saw_touch_event
            else f"no events from {profile.touch_device!r} were found"
        )
        if skipped_incomplete:
            detail = (
                f"{detail} ({skipped_incomplete} incomplete touch frame(s) ignored)"
            )
        raise RecordingError(f"{detail}; record the take again")

    actions: list[MacroAction] = []
    previous_end: float | None = None
    for gesture in gestures:
        action = _gesture_to_contact_group(
            gesture,
            profile,
            previous_end=previous_end,
        )
        ended_at = max(c.ended_at for c in gesture if c.ended_at is not None)
        actions.append(action)
        previous_end = ended_at
    return tuple(actions)


def transform_raw_coordinate(
    raw_x: int,
    raw_y: int,
    profile: DeviceProfile,
) -> tuple[int, int]:
    """Transform a raw touchscreen coordinate into logical display space."""
    match profile.rotation:
        case 0:
            screen_x, screen_y = raw_x, raw_y
        case 1:
            screen_x, screen_y = raw_y, profile.raw_width - raw_x
        case 2:
            screen_x = profile.raw_width - raw_x
            screen_y = profile.raw_height - raw_y
        case 3:
            screen_x, screen_y = profile.raw_height - raw_y, raw_x
        case _:
            raise RecordingError(
                f"unsupported display rotation {profile.rotation}; expected 0-3"
            )

    return (
        min(max(screen_x, 0), profile.logical_width - 1),
        min(max(screen_y, 0), profile.logical_height - 1),
    )


def _sanitize_gesture(
    contacts: tuple[_Contact, ...],
    profile: DeviceProfile,
) -> tuple[_Contact, ...]:
    """Drop ghost / overflow fingers; replay supports at most _MAX_CONTACTS."""
    if len(contacts) <= 1:
        return contacts

    kept = list(contacts)
    changed = True
    while changed and len(kept) > 1:
        changed = False
        for index, contact in enumerate(kept):
            motion = _max_motion(_screen_points(contact, profile))
            is_ghost = (
                contact.duration_s <= _GHOST_MAX_DURATION_S
                and motion <= _GHOST_MAX_MOTION_PX
            )
            if not is_ghost:
                continue
            others_substantial = any(
                j != index
                and (
                    kept[j].duration_s > _GHOST_MAX_DURATION_S * 2.5
                    or _max_motion(_screen_points(kept[j], profile))
                    > _GHOST_MAX_MOTION_PX * 2
                )
                for j in range(len(kept))
            )
            if others_substantial:
                kept.pop(index)
                changed = True
                break

    if len(kept) > _MAX_CONTACTS:
        kept = _keep_top_contacts(kept, profile, limit=_MAX_CONTACTS)
    return tuple(kept)


def _keep_top_contacts(
    contacts: list[_Contact],
    profile: DeviceProfile,
    *,
    limit: int,
) -> list[_Contact]:
    """Keep the most substantial fingers (long hold / more motion), earliest first."""

    def score(contact: _Contact) -> tuple[float, float, float]:
        motion = float(_max_motion(_screen_points(contact, profile)))
        # Prefer long, moving contacts; break ties by earlier down.
        return (contact.duration_s, motion, -contact.down_at)

    ranked = sorted(contacts, key=score, reverse=True)
    chosen = ranked[:limit]
    chosen.sort(key=lambda c: (c.down_at, c.tracking_id))
    return chosen


def _gesture_to_contact_group(
    gesture: tuple[_Contact, ...],
    profile: DeviceProfile,
    *,
    previous_end: float | None,
) -> ContactGroupAction:
    for contact in gesture:
        assert contact.ended_at is not None
    group_start = min(c.down_at for c in gesture)
    ordered = sorted(gesture, key=lambda c: (c.down_at, c.tracking_id))
    samples: list[ContactSample] = []
    for finger_id, contact in enumerate(ordered):
        assert contact.ended_at is not None
        timed = _timed_screen_samples(contact, profile, origin=group_start)
        if not timed:
            continue
        if len(timed) == 1:
            x, y, t0 = timed[0]
            end_ms = max(
                t0,
                _seconds_to_milliseconds(contact.ended_at - group_start),
            )
            samples.append(
                ContactSample(t_ms=t0, finger_id=finger_id, x=x, y=y, phase="down")
            )
            samples.append(
                ContactSample(t_ms=end_ms, finger_id=finger_id, x=x, y=y, phase="up")
            )
            continue
        for index, (x, y, t_ms) in enumerate(timed):
            if index == 0:
                phase: str = "down"
            elif index == len(timed) - 1:
                phase = "up"
            else:
                phase = "move"
            samples.append(
                ContactSample(
                    t_ms=t_ms,
                    finger_id=finger_id,
                    x=x,
                    y=y,
                    phase=phase,  # type: ignore[arg-type]
                )
            )
    if not samples:
        raise RecordingError("contact group produced no samples")
    samples.sort(key=lambda s: (s.t_ms, s.finger_id, _phase_rank(s.phase)))
    return ContactGroupAction(
        delay_ms=_delay_ms(previous_end, group_start),
        samples=tuple(samples),
    )


def _phase_rank(phase: str) -> int:
    if phase == "down":
        return 0
    if phase == "move":
        return 1
    return 2


def _screen_points(
    contact: _Contact,
    profile: DeviceProfile,
) -> tuple[tuple[int, int], ...]:
    timed = _timed_screen_samples(contact, profile, origin=contact.down_at)
    return tuple((x, y) for x, y, _t in timed)


def _timed_screen_samples(
    contact: _Contact,
    profile: DeviceProfile,
    *,
    origin: float,
) -> tuple[tuple[int, int, int], ...]:
    if not contact.samples:
        raise RecordingError("contact has no samples")
    out: list[tuple[int, int, int]] = []
    for ts, raw_x, raw_y in contact.samples:
        x, y = transform_raw_coordinate(raw_x, raw_y, profile)
        t_ms = max(0, _seconds_to_milliseconds(ts - origin))
        if out and out[-1][0] == x and out[-1][1] == y:
            # Keep the earliest time for a stationary stretch. Advancing it
            # delayed finger-down and shortened holds after logical snapping.
            continue
        out.append((x, y, t_ms))
    if contact.ended_at is not None:
        end_ms = max(0, _seconds_to_milliseconds(contact.ended_at - origin))
        if out[-1][2] < end_ms:
            x, y, _ = out[-1]
            out.append((x, y, end_ms))
        else:
            x, y, _ = out[-1]
            out[-1] = (x, y, end_ms)
    return tuple(out)


def _max_motion(points: tuple[tuple[int, int], ...]) -> float:
    if not points:
        return 0.0
    sx, sy = points[0]
    best = 0.0
    for x, y in points:
        dist = ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5
        if dist > best:
            best = dist
    return best


def _delay_ms(previous_end: float | None, started_at: float) -> int:
    if previous_end is None:
        return 0
    return _seconds_to_milliseconds(started_at - previous_end)


def _seed_contact_from_slot_cache(
    contact: _Contact,
    slot: int,
    slot_last_x: dict[int, int],
    slot_last_y: dict[int, int],
) -> None:
    if contact.raw_x is None and slot in slot_last_x:
        contact.raw_x = slot_last_x[slot]
    if contact.raw_y is None and slot in slot_last_y:
        contact.raw_y = slot_last_y[slot]


def _discard_contact(gesture_contacts: list[_Contact], contact: _Contact) -> None:
    try:
        gesture_contacts.remove(contact)
    except ValueError:
        return


def _parse_hex_value(value: str, line_number: int) -> int:
    try:
        parsed = int(value, 16)
    except ValueError as error:
        raise RecordingError(
            f"invalid getevent value {value!r} at trace line {line_number}"
        ) from error
    if parsed >= 1 << 31:
        parsed -= 1 << 32
    return parsed


def _seconds_to_milliseconds(seconds: float) -> int:
    return max(0, round(seconds * 1_000))
