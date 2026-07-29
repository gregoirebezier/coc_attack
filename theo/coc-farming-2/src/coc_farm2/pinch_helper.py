"""Compile and cache the on-device MotionEvent gesture injector."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CommandRunner = Callable[..., subprocess.CompletedProcess[Any]]
_MIN_ANDROID_API = 24


class PinchHelperError(RuntimeError):
    """The Android gesture helper could not be prepared safely."""


@dataclass(frozen=True, slots=True)
class AndroidToolchain:
    javac: Path
    android_jar: Path
    d8: Path


def build_pinch_helper(
    cache_dir: Path,
    *,
    sdk_root: Path | None = None,
    javac_path: Path | None = None,
    runner: CommandRunner = subprocess.run,
    environment: Mapping[str, str] = os.environ,
) -> Path:
    """Build the path+pinch MotionEvent helper (cached by source hash)."""
    source = Path(__file__).with_name("android_helper") / "GestureInjector.java"
    try:
        source_bytes = source.read_bytes()
    except OSError as error:
        raise PinchHelperError(
            f"could not read gesture helper source: {error}"
        ) from error

    digest = hashlib.sha256(
        source_bytes + f"|min-api={_MIN_ANDROID_API}|gesture-v3-timed".encode()
    ).hexdigest()[:16]
    artifact = cache_dir / f"gesture-helper-{digest}.zip"
    if artifact.is_file() and artifact.stat().st_size > 0:
        return artifact

    toolchain = find_android_toolchain(
        sdk_root=sdk_root,
        javac_path=javac_path,
        environment=environment,
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=cache_dir, prefix=".gesture-build-"
    ) as temporary:
        build_dir = Path(temporary)
        classes_dir = build_dir / "classes"
        classes_dir.mkdir()
        _run_checked(
            (
                str(toolchain.javac),
                "-encoding",
                "UTF-8",
                "-source",
                "8",
                "-target",
                "8",
                "-Xlint:-options",
                "-classpath",
                str(toolchain.android_jar),
                "-d",
                str(classes_dir),
                str(source),
            ),
            runner=runner,
            label="javac",
        )
        class_files = tuple(sorted(classes_dir.rglob("*.class")))
        if not class_files:
            raise PinchHelperError("javac produced no helper class files")

        temporary_artifact = build_dir / artifact.name
        _run_checked(
            (
                str(toolchain.d8),
                "--min-api",
                str(_MIN_ANDROID_API),
                "--output",
                str(temporary_artifact),
                *(str(path) for path in class_files),
            ),
            runner=runner,
            label="d8",
        )
        if not temporary_artifact.is_file() or temporary_artifact.stat().st_size == 0:
            raise PinchHelperError("d8 did not produce a usable helper archive")
        os.replace(temporary_artifact, artifact)
    return artifact


def find_android_toolchain(
    *,
    sdk_root: Path | None = None,
    javac_path: Path | None = None,
    environment: Mapping[str, str] = os.environ,
) -> AndroidToolchain:
    selected_sdk = sdk_root or _find_sdk_root(environment)
    if not selected_sdk.is_dir():
        raise PinchHelperError(f"Android SDK was not found at {selected_sdk}")

    android_jars = tuple(
        path / "android.jar"
        for path in sorted((selected_sdk / "platforms").glob("android-*"))
        if (path / "android.jar").is_file()
    )
    if not android_jars:
        raise PinchHelperError("no android.jar found under SDK platforms/")
    android_jar = android_jars[-1]

    d8_candidates = sorted(
        (selected_sdk / "build-tools").glob("*/d8"),
        key=lambda path: path.parent.name,
    )
    if not d8_candidates:
        raise PinchHelperError("d8 not found under SDK build-tools/")
    d8 = d8_candidates[-1]

    javac = javac_path or _find_javac(environment)
    if javac is None or not javac.is_file():
        raise PinchHelperError("javac not found on PATH; install a JDK")

    return AndroidToolchain(javac=javac, android_jar=android_jar, d8=d8)


def _find_sdk_root(environment: Mapping[str, str]) -> Path:
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = environment.get(key)
        if value:
            return Path(value)
    home = Path.home()
    for candidate in (
        home / "Library" / "Android" / "sdk",
        home / "Android" / "Sdk",
    ):
        if candidate.is_dir():
            return candidate
    return home / "Library" / "Android" / "sdk"


def _find_javac(environment: Mapping[str, str]) -> Path | None:
    import shutil

    found = shutil.which("javac", path=environment.get("PATH"))
    return Path(found) if found else None


def _run_checked(
    command: Sequence[str],
    *,
    runner: CommandRunner,
    label: str,
) -> None:
    completed = runner(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise PinchHelperError(f"{label} failed: {detail}")
