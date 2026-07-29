"""Command-line interface for coc-farm2."""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal

from coc_farm2.adb import AdbClient, AdbError
from coc_farm2.alerts import TerminalBellAlert
from coc_farm2.calibration import (
    CalibrationError,
    recommended_home_point,
    sample_probe_from_live,
    select_point,
    select_rect,
)
from coc_farm2.capture import CaptureError, capture_getevent_trace
from coc_farm2.checkpoints import (
    CheckpointError,
    insert_checkpoint,
    unguarded_long_gestures,
)
from coc_farm2.controls import OperatorControls
from coc_farm2.doctor import DEFAULT_SERIAL, DoctorExpectations, run_doctor
from coc_farm2.live import LiveProbeReader
from coc_farm2.lock import RunLock, RunLockError
from coc_farm2.loot import format_loot, meets_thresholds
from coc_farm2.models import (
    FarmConfig,
    LootThresholds,
    Macro,
    OcrRegion,
    RecordedTake,
    WaitPixelAction,
    WaitPixelsAction,
)
from coc_farm2.ocr import OcrError, create_ocr_backend, read_loot
from coc_farm2.pinch_helper import PinchHelperError, build_pinch_helper
from coc_farm2.preview import render_macro_preview
from coc_farm2.recording import MultitouchError, RecordingError, parse_getevent_trace
from coc_farm2.replay import (
    macro_needs_pinch,
    macro_needs_probes,
    replay_macro,
)
from coc_farm2.runner import CycleOutcome, FarmingRunner, RunnerFault, RunnerPaused
from coc_farm2.storage import ProjectStore, StorageError
from coc_farm2.timing import apply_timing
from coc_farm2.variation import vary_macro


class CliError(RuntimeError):
    """Operator-facing workflow failure."""


REQUIRED_PROBES = (
    "home",
    "gold-full",
    "elixir-full",
    "match_ready_a",
    "match_ready_b",
    "return_ready_a",
    "return_ready_b",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coc-farm2",
        description=(
            "Phased CoC farming over USB ADB: search, loot gate (OCR), "
            "multi-template attack with Gaussian jitter, return home."
        ),
    )
    parser.add_argument(
        "--serial",
        default=DEFAULT_SERIAL,
        help=f"ADB device serial (default: {DEFAULT_SERIAL})",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(".coc-farm2"),
        help="runtime profile, calibration, and log directory",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="verify and save the exact device profile")
    commands.add_parser("status", help="show which manual setup steps remain")

    calibrate = commands.add_parser(
        "calibrate",
        help="calibrate a named pixel probe from a live screenshot",
    )
    calibrate.add_argument(
        "name",
        choices=[
            "home",
            "gold-full",
            "elixir-full",
            "match_ready_a",
            "match_ready_b",
            "return_ready_a",
            "return_ready_b",
            "next_button",
            "return_tap",
        ],
    )

    calibrate_ocr = commands.add_parser(
        "calibrate-ocr",
        help="calibrate an OCR crop rectangle (two corners)",
    )
    calibrate_ocr.add_argument("name", choices=["gold", "elixir", "dark"])

    thresholds = commands.add_parser("set-threshold", help="set loot thresholds")
    thresholds.add_argument("--gold", type=int)
    thresholds.add_argument("--elixir", type=int)
    thresholds.add_argument("--dark", type=int)
    thresholds.add_argument("--sum", dest="sum_threshold", type=int)
    thresholds.add_argument("--mode", choices=["all", "sum"])

    record = commands.add_parser("record", help="record a phase macro from the phone")
    record.add_argument("phase", choices=["start_search", "attack"])

    checkpoint = commands.add_parser(
        "checkpoint",
        help="add a pixel wait before a long-delay gesture",
    )
    checkpoint.add_argument("macro", help="start_search or attack:<id>")
    checkpoint.add_argument("--before-gesture", type=int, required=True)
    checkpoint.add_argument("--probe-name", required=True)
    checkpoint.add_argument("--timeout-ms", type=int)

    preview = commands.add_parser(
        "preview",
        help="open a numbered gesture overlay and/or replay on device",
    )
    preview.add_argument("macro", help="start_search or attack:<id>")
    preview.add_argument(
        "--live",
        action="store_true",
        help="replay the macro touches on the connected device",
    )
    preview.add_argument(
        "--no-overlay",
        action="store_true",
        help="skip writing/opening the PNG overlay (useful with --live)",
    )
    preview.add_argument(
        "--yes",
        action="store_true",
        help="with --live, skip the PLAY confirmation prompt",
    )
    preview.add_argument(
        "--jitter",
        action="store_true",
        help="with --live, apply Gaussian coordinate/timing jitter from config",
    )
    preview.add_argument(
        "--seed",
        type=int,
        help="with --live --jitter, RNG seed for reproducible jitter",
    )

    delete = commands.add_parser(
        "delete",
        help="delete a recorded macro / attack template and its artifacts",
    )
    delete.add_argument("macro", help="start_search or attack:<id>")
    delete.add_argument(
        "--yes",
        action="store_true",
        help="skip the DELETE confirmation prompt",
    )

    recompile = commands.add_parser(
        "recompile",
        help="re-parse a saved getevent take into a macro (after parser fixes)",
    )
    recompile.add_argument("macro", help="start_search or attack:<id>")

    approve = commands.add_parser("approve", help="approve a macro for live use")
    approve.add_argument("macro", help="start_search or attack:<id>")

    commands.add_parser("ocr-test", help="screenshot and print OCR loot reading")

    test_once = commands.add_parser(
        "test-once",
        help="run one supervised farming cycle",
    )
    test_once.add_argument("--seed", type=int)

    run_cmd = commands.add_parser("run", help="run the farming loop")
    run_cmd.add_argument("--max-cycles", type=int)
    run_cmd.add_argument("--seed", type=int)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = ProjectStore(args.data_dir)
    try:
        if args.command == "doctor":
            return cmd_doctor(args.serial, store)
        if args.command == "status":
            return cmd_status(store)
        if args.command == "calibrate":
            return cmd_calibrate(args.serial, store, args.name)
        if args.command == "calibrate-ocr":
            return cmd_calibrate_ocr(args.serial, store, args.name)
        if args.command == "set-threshold":
            return cmd_set_threshold(
                store,
                args.gold,
                args.elixir,
                args.dark,
                args.sum_threshold,
                args.mode,
            )
        if args.command == "record":
            return cmd_record(args.serial, store, args.phase)
        if args.command == "checkpoint":
            return cmd_checkpoint(
                store,
                args.macro,
                args.before_gesture,
                args.probe_name,
                args.timeout_ms,
            )
        if args.command == "preview":
            return cmd_preview(
                args.serial,
                store,
                args.macro,
                live=args.live,
                no_overlay=args.no_overlay,
                yes=args.yes,
                jitter=args.jitter,
                seed=args.seed,
            )
        if args.command == "delete":
            return cmd_delete(store, args.macro, yes=args.yes)
        if args.command == "recompile":
            return cmd_recompile(store, args.macro)
        if args.command == "approve":
            return cmd_approve(store, args.macro)
        if args.command == "ocr-test":
            return cmd_ocr_test(args.serial, store)
        if args.command == "test-once":
            return cmd_run(
                args.serial,
                store,
                max_cycles=1,
                seed=args.seed,
                supervised=True,
            )
        if args.command == "run":
            return cmd_run(
                args.serial,
                store,
                max_cycles=args.max_cycles,
                seed=args.seed,
                supervised=False,
            )
        parser.error(f"unknown command {args.command}")
    except (CliError, StorageError, AdbError, CalibrationError, OcrError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(serial: str, store: ProjectStore) -> int:
    client = AdbClient(serial)
    report = run_doctor(client, DoctorExpectations(serial=serial))
    for check in report.checks:
        mark = "OK" if check.passed else "FAIL"
        crit = "" if check.critical else " (warn)"
        print(f"[{mark}]{crit} {check.name}: {check.detail}")
    if not report.ok or report.profile is None:
        print("doctor failed — fix the FAIL items and retry", file=sys.stderr)
        return 1
    path = store.save_profile(report.profile)
    store.load_config()  # ensure defaults exist
    print(f"saved device profile → {path}")
    return 0


def cmd_status(store: ProjectStore) -> int:
    missing: list[str] = []
    if not store.profile_exists():
        missing.append("run: coc-farm2 doctor")
    probes = store.load_probes() if store.profile_exists() else {}
    for name in REQUIRED_PROBES:
        if name not in probes:
            missing.append(f"calibrate probe: coc-farm2 calibrate {name}")
    config = store.load_config() if store.profile_exists() else FarmConfig()
    if config.next_button is None:
        missing.append("calibrate: coc-farm2 calibrate next_button")
    if config.return_tap is None:
        missing.append("calibrate: coc-farm2 calibrate return_tap")
    regions = store.load_ocr_regions() if store.profile_exists() else {}
    if "gold" not in regions:
        missing.append("calibrate OCR: coc-farm2 calibrate-ocr gold")
    if "elixir" not in regions:
        missing.append("calibrate OCR: coc-farm2 calibrate-ocr elixir")
    if not store.macro_exists("start_search"):
        missing.append("record: coc-farm2 record start_search")
    else:
        macro = store.load_macro("start_search")
        if not macro.approved:
            missing.append("approve: coc-farm2 approve start_search")
        for gesture in unguarded_long_gestures(
            macro.actions,
            threshold_ms=config.long_gesture_threshold_ms,
        ):
            missing.append(
                f"checkpoint start_search gesture {gesture}: "
                f"coc-farm2 checkpoint start_search --before-gesture {gesture} "
                f"--probe-name <name>"
            )
    attacks = store.load_attack_macros()
    if len(attacks) < 1:
        missing.append("record at least one attack: coc-farm2 record attack")
    approved = [a for a in attacks if a.approved]
    if attacks and not approved:
        missing.append("approve attacks: coc-farm2 approve attack:<id>")

    if not missing:
        print("ready — all setup steps complete")
        print(
            f"thresholds gold={config.thresholds.gold:,} "
            f"elixir={config.thresholds.elixir:,} dark={config.thresholds.dark:,}"
        )
        if config.loot_mode == "sum":
            print(f"loot mode=sum threshold={config.sum_threshold:,}")
        else:
            print("loot mode=all")
        print(f"attack templates approved: {len(approved)}")
        return 0

    print("setup incomplete:")
    for item in missing:
        print(f"  - {item}")
    return 2


def cmd_calibrate(serial: str, store: ProjectStore, name: str) -> int:
    profile = store.load_profile()
    client = AdbClient(serial)
    client.bring_to_front(profile.package, profile.activity)
    screenshot = client.screenshot()
    store.calibration_dir.mkdir(parents=True, exist_ok=True)
    shot_path = store.calibration_dir / f"{name}.png"
    screenshot.save(shot_path)

    if name == "home":
        point = recommended_home_point(profile.app_bounds)
        print(f"using automatic home point ({point.x}, {point.y})")
    elif name in {"next_button", "return_tap"}:
        print(f"open the browser and click the {name.replace('_', ' ')}")
        point = select_point(screenshot)
        config = store.load_config()
        if name == "next_button":
            config = replace(config, next_button=point)
        else:
            config = replace(config, return_tap=point)
        store.save_config(config)
        print(f"saved {name} → ({point.x}, {point.y})")
        return 0
    else:
        print(f"open the browser and click a stable pixel for {name}")
        point = select_point(screenshot)

    probe = sample_probe_from_live(
        name,
        point.x,
        point.y,
        client.screenshot,
    )
    probes = store.load_probes()
    probes[name] = probe
    store.save_probes(probes)
    print(
        f"saved probe {name} @ ({probe.x}, {probe.y}) "
        f"rgb={probe.reference_rgb} tol={probe.tolerance}"
    )
    return 0


def cmd_calibrate_ocr(serial: str, store: ProjectStore, name: str) -> int:
    profile = store.load_profile()
    client = AdbClient(serial)
    client.bring_to_front(profile.package, profile.activity)
    screenshot = client.screenshot()
    print(f"click two opposite corners of the {name} loot number region")
    rect = select_rect(screenshot)
    regions = store.load_ocr_regions()
    regions[name] = OcrRegion(name=name, rect=rect)
    store.save_ocr_regions(regions)
    shot_path = store.calibration_dir / f"ocr-{name}.png"
    store.calibration_dir.mkdir(parents=True, exist_ok=True)
    screenshot.crop((rect.left, rect.top, rect.right, rect.bottom)).save(shot_path)
    print(f"saved OCR region {name}: {rect.to_dict()} (crop → {shot_path})")
    return 0


def cmd_set_threshold(
    store: ProjectStore,
    gold: int | None,
    elixir: int | None,
    dark: int | None,
    sum_threshold: int | None,
    mode: Literal["all", "sum"] | None,
) -> int:
    if (
        gold is None
        and elixir is None
        and dark is None
        and sum_threshold is None
        and mode is None
    ):
        raise CliError("provide --mode and/or at least one threshold")
    config = store.load_config()
    thresholds = LootThresholds(
        gold=config.thresholds.gold if gold is None else gold,
        elixir=config.thresholds.elixir if elixir is None else elixir,
        dark=config.thresholds.dark if dark is None else dark,
    )
    loot_mode = config.loot_mode if mode is None else mode
    configured_sum = config.sum_threshold if sum_threshold is None else sum_threshold
    if loot_mode == "sum" and configured_sum <= 0:
        raise CliError("--sum must be positive in sum mode")
    store.save_config(
        replace(
            config,
            thresholds=thresholds,
            loot_mode=loot_mode,
            sum_threshold=configured_sum,
        )
    )
    print(
        f"thresholds gold={thresholds.gold:,} "
        f"elixir={thresholds.elixir:,} dark={thresholds.dark:,}"
    )
    if loot_mode == "sum":
        print(f"loot mode=sum threshold={configured_sum:,}")
    else:
        print("loot mode=all")
    return 0


def cmd_record(serial: str, store: ProjectStore, phase: str) -> int:
    profile = store.load_profile()
    client = AdbClient(serial)
    client.bring_to_front(profile.package, profile.activity)

    if phase == "start_search":
        macro_name = "start_search"
        take_number = 1
        print("Record start_search: home village → Find a Match screen.")
    else:
        macro_name = "attack"
        take_number = store.next_take_number("attack")
        print(
            f"Record attack take {take_number:02d}: "
            "already on a base → deploy troops (no Return)."
        )

    input("Position the game, then press Enter to start a 3s countdown… ")
    for second in (3, 2, 1):
        print(second)
        time.sleep(1)
    print("recording — perform gestures on the phone, then press Enter here to stop")

    try:

        def wait_for_stop() -> None:
            input()

        trace = capture_getevent_trace(
            profile,
            wait_for_stop=wait_for_stop,
            client=client,
        )
    except CaptureError as error:
        raise CliError(str(error)) from error

    trace_path = store.take_trace_path(macro_name, take_number)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    # Always keep the raw stream so a parse failure can be inspected / retried.
    failed_trace_path = (
        store.root / "takes" / macro_name / f"{take_number:02d}.getevent.failed.txt"
    )
    try:
        actions = parse_getevent_trace(trace, profile)
    except (RecordingError, MultitouchError) as error:
        failed_trace_path.write_text(trace, encoding="utf-8")
        raise CliError(f"{error}\nraw getevent saved → {failed_trace_path}") from error

    take_id = f"{take_number:02d}"
    take = RecordedTake(name=take_id, profile=profile, actions=actions)
    store.save_take(macro_name, take_number, take)
    trace_path.write_text(trace, encoding="utf-8")
    failed_trace_path.unlink(missing_ok=True)

    macro = Macro(
        name=f"{macro_name}-{take_id}" if macro_name == "attack" else macro_name,
        profile=profile,
        actions=actions,
        approved=False,
        source_take_name=take_id,
    )
    if macro_name == "attack":
        path = store.save_attack_macro(take_id, macro)
    else:
        path = store.save_macro(macro)

    long_ones = unguarded_long_gestures(
        actions,
        threshold_ms=store.load_config().long_gesture_threshold_ms,
    )
    print(f"saved {len(actions)} actions → {path}")
    if long_ones:
        print(
            "unguarded long delays at gesture(s): "
            + ", ".join(str(n) for n in long_ones)
        )
        print("add checkpoints before approving")
    return 0


def _resolve_macro(store: ProjectStore, spec: str) -> tuple[str, Macro, Path | None]:
    if spec == "start_search":
        return "start_search", store.load_macro("start_search"), None
    if spec.startswith("attack:"):
        take_id = spec.split(":", 1)[1]
        path = store.root / "macros" / "attacks" / f"{take_id}.json"
        macro = Macro.from_dict(
            __import__("json").loads(path.read_text(encoding="utf-8"))
        )
        return take_id, macro, path
    raise CliError(f"unknown macro spec {spec!r}; use start_search or attack:<id>")


def cmd_checkpoint(
    store: ProjectStore,
    macro_spec: str,
    before_gesture: int,
    probe_name: str,
    timeout_ms: int | None,
) -> int:
    probes = store.load_probes()
    if probe_name not in probes:
        raise CliError(f"probe {probe_name!r} is not calibrated yet")
    kind, macro, attack_path = _resolve_macro(store, macro_spec)
    try:
        updated = insert_checkpoint(
            macro,
            before_gesture=before_gesture,
            probe_name=probe_name,
            timeout_ms=timeout_ms,
        )
    except CheckpointError as error:
        raise CliError(str(error)) from error
    if attack_path is not None:
        store.save_attack_macro(kind, updated)
    else:
        store.save_macro(updated)
    print(f"inserted checkpoint {probe_name!r} before gesture {before_gesture}")
    return 0


def cmd_preview(
    serial: str,
    store: ProjectStore,
    macro_spec: str,
    *,
    live: bool = False,
    no_overlay: bool = False,
    yes: bool = False,
    jitter: bool = False,
    seed: int | None = None,
) -> int:
    if jitter and not live:
        raise CliError("--jitter requires --live")
    if seed is not None and not jitter:
        raise CliError("--seed requires --live --jitter")

    _, macro, _ = _resolve_macro(store, macro_spec)
    client = AdbClient(serial)

    if not no_overlay:
        try:
            background = client.screenshot()
        except AdbError:
            background = None
        out = store.previews_dir / f"{macro.name}.png"
        render_macro_preview(macro, background, output_path=out)
        print(f"wrote {out}")
        webbrowser.open(out.resolve().as_uri())

    if not live:
        if no_overlay:
            raise CliError("nothing to do: pass --live and/or omit --no-overlay")
        return 0

    profile = store.load_profile()
    if macro.profile != profile:
        raise CliError(
            "macro was recorded with a different device profile than doctor; "
            "re-record or re-run doctor"
        )

    client.bring_to_front(profile.package, profile.activity)

    # Contact timelines need the on-device MotionEvent helper.
    try:
        helper = build_pinch_helper(store.helper_dir)
        client.install_pinch_helper(helper)
    except PinchHelperError as error:
        if macro_needs_pinch(macro):
            raise CliError(f"gesture helper: {error}") from error
        print(f"warning: gesture helper unavailable ({error})")

    probes = store.load_probes()
    live_reader: LiveProbeReader | None = None
    probe_reader = None
    probe_group_reader = None
    if macro_needs_probes(macro):
        missing = []
        for action in macro.actions:
            if isinstance(action, WaitPixelAction):
                if action.probe_name not in probes:
                    missing.append(action.probe_name)
            elif isinstance(action, WaitPixelsAction):
                missing.extend(
                    name for name in action.probe_names if name not in probes
                )
        if missing:
            raise CliError(
                "macro has pixel checkpoints but probe(s) not calibrated: "
                + ", ".join(sorted(set(missing)))
            )
        live_reader = LiveProbeReader(client.screenshot, inter_frame_sleeper=time.sleep)
        probe_reader = live_reader.matches
        probe_group_reader = live_reader.matches_group

    config = store.load_config()
    play_macro = apply_timing(macro, config.timing)
    jitter_note = "no Gaussian jitter"
    if jitter:
        rng = random.Random(seed)
        play_macro = vary_macro(play_macro, config.variation, rng=rng)
        jitter_note = (
            f"Gaussian jitter "
            f"(coord_sigma={config.variation.coord_sigma_px}px, "
            f"delay_sigma={config.variation.delay_sigma_ms}ms"
            + (f", seed={seed}" if seed is not None else "")
            + ")"
        )

    print(
        f"Live replay of {macro.name!r}: {len(play_macro.actions)} action(s), "
        f"{jitter_note}, delay_scale={config.timing.delay_scale}."
    )
    print(
        "Put the game on the same starting screen as when you recorded, then confirm."
    )
    if not yes:
        confirm = input("Type PLAY to inject touches on the device: ").strip()
        if confirm != "PLAY":
            print("aborted")
            return 1

    try:
        # Open lazily on first tap; do not hold the shell open during preflight
        # screenshots (concurrent adb often hangs screencap on USB devices).
        replay_macro(
            client,
            play_macro,
            probes=probes,
            probe_reader=probe_reader,
            probe_group_reader=probe_group_reader,
            probe_cache_invalidator=(
                live_reader.invalidate if live_reader is not None else lambda: None
            ),
            on_log=lambda msg: print(f"[replay] {msg}"),
        )
    except RunnerFault as error:
        raise CliError(f"live preview stopped: {error}") from error
    finally:
        client.close_input_shell()

    print("live replay finished")
    return 0


def cmd_recompile(store: ProjectStore, macro_spec: str) -> int:
    """Re-parse a saved getevent trace with the current parser and overwrite macro."""
    profile = store.load_profile()
    if macro_spec == "start_search":
        take_number = 1
        macro_name = "start_search"
        take_id = "01"
        trace_path = store.take_trace_path("start_search", take_number)
    elif macro_spec.startswith("attack:"):
        take_id = macro_spec.split(":", 1)[1]
        try:
            take_number = int(take_id)
        except ValueError as error:
            raise CliError(f"invalid attack id {take_id!r}") from error
        macro_name = "attack"
        trace_path = store.take_trace_path("attack", take_number)
    else:
        raise CliError(
            f"unknown macro spec {macro_spec!r}; use start_search or attack:<id>"
        )

    failed_trace_path = (
        store.root / "takes" / macro_name / f"{take_number:02d}.getevent.failed.txt"
    )
    source_trace = trace_path if trace_path.is_file() else failed_trace_path
    if not source_trace.is_file():
        raise CliError(f"missing getevent trace: {trace_path}")

    try:
        actions = parse_getevent_trace(
            source_trace.read_text(encoding="utf-8"),
            profile,
        )
    except (RecordingError, MultitouchError) as error:
        raise CliError(str(error)) from error

    # Promote a recovered failed capture to the canonical trace path.
    if source_trace != trace_path:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        text = source_trace.read_text(encoding="utf-8")
        trace_path.write_text(text, encoding="utf-8")
        failed_trace_path.unlink(missing_ok=True)

    take = RecordedTake(name=take_id, profile=profile, actions=actions)
    store.save_take(macro_name, take_number, take)
    macro = Macro(
        name=f"{macro_name}-{take_id}" if macro_name == "attack" else macro_name,
        profile=profile,
        actions=actions,
        approved=False,
        source_take_name=take_id,
    )
    if macro_name == "attack":
        path = store.save_attack_macro(take_id, macro)
    else:
        path = store.save_macro(macro)

    from collections import Counter

    counts = Counter(action.kind for action in actions)
    print(f"recompiled {macro_spec} → {path}")
    print("  actions: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("  approval cleared — re-run preview --live then approve")
    return 0


def cmd_delete(store: ProjectStore, macro_spec: str, *, yes: bool = False) -> int:
    if macro_spec == "start_search":
        label = "start_search"
    elif macro_spec.startswith("attack:"):
        take_id = macro_spec.split(":", 1)[1]
        if not take_id:
            raise CliError("attack id is required, e.g. attack:01")
        label = f"attack template {take_id}"
    else:
        raise CliError(
            f"unknown macro spec {macro_spec!r}; use start_search or attack:<id>"
        )

    print(f"This will permanently delete {label} artifacts under {store.root}")
    if not yes:
        confirm = input("Type DELETE to confirm: ").strip()
        if confirm != "DELETE":
            print("aborted")
            return 1

    if macro_spec == "start_search":
        removed = store.delete_start_search()
    else:
        take_id = macro_spec.split(":", 1)[1]
        removed = store.delete_attack(take_id)

    print(f"deleted {len(removed)} file(s):")
    for path in removed:
        print(f"  - {path}")
    return 0


def cmd_approve(store: ProjectStore, macro_spec: str) -> int:
    if macro_spec == "start_search":
        macro = store.approve_macro("start_search")
        print(f"approved {macro.name}")
        return 0
    if macro_spec.startswith("attack:"):
        take_id = macro_spec.split(":", 1)[1]
        macro = store.approve_attack(take_id)
        print(f"approved attack {macro.name}")
        return 0
    raise CliError(f"unknown macro spec {macro_spec!r}")


def cmd_ocr_test(serial: str, store: ProjectStore) -> int:
    profile = store.load_profile()
    regions = store.load_ocr_regions()
    if not regions:
        raise CliError("no OCR regions calibrated")
    client = AdbClient(serial)
    client.bring_to_front(profile.package, profile.activity)
    image = client.screenshot()
    try:
        backend, desc = create_ocr_backend()
    except OcrError as error:
        raise CliError(str(error)) from error
    print(f"OCR backend: {desc}")
    backend.warmup()
    import time as _time

    t0 = _time.perf_counter()
    reading = read_loot(image, regions, backend)
    elapsed_ms = (_time.perf_counter() - t0) * 1000
    config = store.load_config()
    print(format_loot(reading))
    print(f"OCR time: {elapsed_ms:.0f} ms")
    print(
        "meets thresholds:",
        meets_thresholds(
            reading,
            config.thresholds,
            mode=config.loot_mode,
            sum_threshold=config.sum_threshold,
        ),
    )
    return 0


def cmd_run(
    serial: str,
    store: ProjectStore,
    *,
    max_cycles: int | None,
    seed: int | None,
    supervised: bool,
) -> int:
    status_code = cmd_status(store)
    if status_code != 0:
        raise CliError("setup incomplete — see status above")

    profile = store.load_profile()
    config = store.load_config()
    probes = store.load_probes()
    regions = store.load_ocr_regions()
    start_search = store.load_macro("start_search")
    attacks = store.load_approved_attack_macros()
    client = AdbClient(serial)
    client.bring_to_front(profile.package, profile.activity)

    # MotionEvent helper for path swipes + pinches (always preferred).
    try:
        helper = build_pinch_helper(store.helper_dir)
        client.install_pinch_helper(helper)
    except PinchHelperError as error:
        raise CliError(f"gesture helper: {error}") from error

    if supervised:
        confirm = input("Type RUN to start one supervised cycle: ").strip()
        if confirm != "RUN":
            print("aborted")
            return 1
    else:
        confirm = input("Type START to begin farming (ban risk): ").strip()
        if confirm != "START":
            print("aborted")
            return 1

    # Fast probes: 1 frame, no inter-frame sleep (calibration still used multi-frame).
    live = LiveProbeReader(
        client.screenshot,
        inter_frame_sleeper=time.sleep,
        frame_gap_s=0.0,
        sample_count=1,
    )
    stop_event = threading.Event()
    pause_event = threading.Event()
    controls = OperatorControls(
        stop_event=stop_event,
        pause_event=pause_event,
        on_message=lambda msg: print(f"[controls] {msg}"),
    )
    controls.start()
    alert = TerminalBellAlert()
    rng = random.Random(seed)

    try:
        ocr_backend, ocr_desc = create_ocr_backend()
    except OcrError as error:
        raise CliError(str(error)) from error
    print(f"[farm] OCR backend: {ocr_desc} (warming up…)")
    ocr_backend.warmup()
    print("[farm] OCR ready")

    def recover_system_interruption() -> str | None:
        try:
            return client.dismiss_foreign_dialog(profile.package)
        except AdbError:
            return None

    runner = FarmingRunner(
        device=client,
        profile=profile,
        config=config,
        probes=probes,
        ocr_regions=regions,
        start_search=start_search,
        attack_templates=attacks,
        ocr_backend=ocr_backend,
        probe_reader=live.matches,
        probe_group_reader=live.matches_group,
        probe_batch_reader=live.matches_many,
        probe_frame_reader=live.matches_many_in,
        probe_cache_invalidator=live.invalidate,
        input_shell_releaser=client.close_input_shell,
        interruption_recoverer=recover_system_interruption,
        stop_event=stop_event,
        pause_event=pause_event,
        safety_interval_s=5.0,
        poll_interval_s=0.1,
        rng=rng,
        on_log=lambda msg: print(f"[farm] {msg}"),
    )

    cycles = 0
    try:
        # Input shell opens on first inject; closed around every screencap/dumpsys
        # so preflight probes cannot hang the USB transport.
        print("[farm] starting cycle (input shell opens on first gesture)")
        with RunLock(store.lock_path):
            while max_cycles is None or cycles < max_cycles:
                try:
                    outcome = runner.run_cycle()
                except RunnerPaused:
                    print("paused — fix state if needed, press Enter to retry cycle")
                    pause_event.clear()
                    input()
                    continue
                except RunnerFault as error:
                    print(f"fault: {error}", file=sys.stderr)
                    try:
                        shot = client.screenshot()
                        fault_path = store.runs_dir / f"fault-{int(time.time())}.png"
                        store.runs_dir.mkdir(parents=True, exist_ok=True)
                        shot.save(fault_path)
                        print(f"screenshot → {fault_path}")
                    except AdbError:
                        pass
                    print("restore home village, then Enter to retry or q to quit")
                    response = input().strip().lower()
                    if response in {"q", "quit"}:
                        return 1
                    continue

                cycles += 1
                print(f"cycle {cycles} → {outcome.value}")
                if outcome == CycleOutcome.RESOURCES_FULL:
                    alert.announce(
                        "gold or elixir reserve is full — upgrade, then resume"
                    )
                    controls.stop()
                    try:
                        if not alert.wait_for_resume(
                            should_continue_alerting=runner.resources_full,
                        ):
                            return 0
                    finally:
                        controls.start()
                if supervised:
                    break
    except RunLockError as error:
        raise CliError(str(error)) from error
    finally:
        client.close_input_shell()
        controls.stop()

    print(f"done ({cycles} cycle(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
