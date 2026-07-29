"""Deterministic macro replay on a live device (preview / dry-run)."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import assert_never

from coc_farm2.models import (
    ContactGroupAction,
    DeviceProfile,
    Macro,
    MacroAction,
    PixelProbe,
    WaitAction,
    WaitPixelAction,
    WaitPixelsAction,
)
from coc_farm2.runner import RunnerDevice, RunnerFault, validate_safety_status
from coc_farm2.session import session_duration_ms


def replay_macro(
    device: RunnerDevice,
    macro: Macro,
    *,
    probes: Mapping[str, PixelProbe] | None = None,
    probe_reader: Callable[[PixelProbe], bool] | None = None,
    probe_group_reader: Callable[[Sequence[PixelProbe]], bool] | None = None,
    probe_cache_invalidator: Callable[[], None] = lambda: None,
    check_safety: bool = True,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    poll_interval_s: float = 0.5,
    on_log: Callable[[str], None] = lambda _msg: None,
) -> None:
    """
    Replay a macro exactly (no Gaussian variation).

    Consecutive contact groups are batched into one on-device session so gaps
    are slept inside a single JVM (not host-sleep + app_process per group).
    """
    profile = macro.profile
    probe_map = dict(probes or {})

    def ensure_safety() -> None:
        if not check_safety:
            return
        status = device.safety_status(profile)
        validate_safety_status(status, profile)

    batch: list[ContactGroupAction] = []
    total = len(macro.actions)

    def flush_batch(end_index: int) -> None:
        if not batch:
            return
        start_index = end_index - len(batch) + 1
        for group in batch:
            for sample in group.samples:
                _validate_point(profile, sample.x, sample.y)
        on_log(
            f"[{start_index}-{end_index}/{total}] session "
            f"{len(batch)} groups {session_duration_ms(batch)}ms"
        )
        ensure_safety()
        device.inject_contact_session(tuple(batch), profile)
        probe_cache_invalidator()
        batch.clear()

    for index, action in enumerate(macro.actions, start=1):
        if isinstance(action, ContactGroupAction):
            batch.append(action)
            continue
        flush_batch(index - 1)
        if action.delay_ms:
            sleeper(action.delay_ms / 1000)
        ensure_safety()
        _dispatch_non_contact(
            device,
            action,
            index=index,
            total=total,
            probes=probe_map,
            probe_reader=probe_reader,
            probe_group_reader=probe_group_reader,
            sleeper=sleeper,
            monotonic=monotonic,
            poll_interval_s=poll_interval_s,
            on_log=on_log,
            ensure_safety=ensure_safety,
        )
    flush_batch(total)


def _dispatch_non_contact(
    device: RunnerDevice,
    action: MacroAction,
    *,
    index: int,
    total: int,
    probes: Mapping[str, PixelProbe],
    probe_reader: Callable[[PixelProbe], bool] | None,
    probe_group_reader: Callable[[Sequence[PixelProbe]], bool] | None,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
    poll_interval_s: float,
    on_log: Callable[[str], None],
    ensure_safety: Callable[[], None],
) -> None:
    if isinstance(action, WaitAction):
        on_log(f"[{index}/{total}] wait {action.duration_ms}ms")
        sleeper(action.duration_ms / 1000)
        return
    if isinstance(action, WaitPixelAction):
        probe = probes.get(action.probe_name)
        if probe is None:
            raise RunnerFault(f"macro references missing probe {action.probe_name!r}")
        if probe_reader is None:
            raise RunnerFault("pixel waits require a probe reader")
        on_log(
            f"[{index}/{total}] wait_pixel {action.probe_name!r} "
            f"(timeout {action.timeout_ms}ms)"
        )
        _wait_for_probe(
            probe,
            probe_reader=probe_reader,
            timeout_s=action.timeout_ms / 1000,
            sleeper=sleeper,
            monotonic=monotonic,
            poll_interval_s=poll_interval_s,
            ensure_safety=ensure_safety,
        )
        return
    if isinstance(action, WaitPixelsAction):
        missing = [name for name in action.probe_names if name not in probes]
        if missing:
            raise RunnerFault(
                f"macro references missing probe(s): {', '.join(missing)}"
            )
        if probe_group_reader is None:
            raise RunnerFault("multi-pixel waits require a grouped probe reader")
        selected = tuple(probes[name] for name in action.probe_names)
        on_log(
            f"[{index}/{total}] wait_pixels "
            f"{list(action.probe_names)} (timeout {action.timeout_ms}ms)"
        )
        _wait_for_probes(
            selected,
            probe_group_reader=probe_group_reader,
            timeout_s=action.timeout_ms / 1000,
            sleeper=sleeper,
            monotonic=monotonic,
            poll_interval_s=poll_interval_s,
            ensure_safety=ensure_safety,
        )
        return
    if isinstance(action, ContactGroupAction):
        raise RunnerFault("contact groups must be flushed via session batching")
    assert_never(action)


def _wait_for_probe(
    probe: PixelProbe,
    *,
    probe_reader: Callable[[PixelProbe], bool],
    timeout_s: float,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
    poll_interval_s: float,
    ensure_safety: Callable[[], None],
) -> None:
    deadline = monotonic() + timeout_s
    while True:
        ensure_safety()
        if probe_reader(probe):
            return
        if monotonic() >= deadline:
            raise RunnerFault(f"timed out waiting for pixel checkpoint {probe.name!r}")
        sleeper(poll_interval_s)


def _wait_for_probes(
    probes: Sequence[PixelProbe],
    *,
    probe_group_reader: Callable[[Sequence[PixelProbe]], bool],
    timeout_s: float,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
    poll_interval_s: float,
    ensure_safety: Callable[[], None],
) -> None:
    deadline = monotonic() + timeout_s
    while True:
        ensure_safety()
        if probe_group_reader(probes):
            return
        if monotonic() >= deadline:
            names = ", ".join(repr(probe.name) for probe in probes)
            raise RunnerFault(
                f"timed out waiting for grouped pixel checkpoints {names}"
            )
        sleeper(poll_interval_s)


def _validate_point(profile: DeviceProfile, x: int, y: int) -> None:
    if not profile.app_bounds.contains(x, y):
        raise RunnerFault(f"point ({x}, {y}) is outside the recorded app bounds")


def macro_needs_helper(macro: Macro) -> bool:
    """True when the on-device MotionEvent helper is required."""
    return any(isinstance(action, ContactGroupAction) for action in macro.actions)


macro_needs_pinch = macro_needs_helper


def macro_needs_probes(macro: Macro) -> bool:
    return any(
        isinstance(action, (WaitPixelAction, WaitPixelsAction))
        for action in macro.actions
    )
