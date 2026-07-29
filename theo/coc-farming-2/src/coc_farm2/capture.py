"""Live getevent capture sessions."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from coc_farm2.adb import AdbClient, AdbError
from coc_farm2.models import DeviceProfile


class CaptureError(RuntimeError):
    """Raised when the live touchscreen stream cannot be captured."""


PopenFactory = Callable[..., Any]


def capture_getevent_trace(
    profile: DeviceProfile,
    *,
    wait_for_stop: Callable[[], None],
    adb_path: str = "adb",
    popen_factory: PopenFactory | None = None,
    client: AdbClient | None = None,
) -> str:
    if client is not None and client.serial != profile.serial:
        raise CaptureError(
            f"ADB client is bound to {client.serial!r}, "
            f"but the recording profile expects {profile.serial!r}"
        )
    if client is None:
        client_options: dict[str, Any] = {"adb_path": adb_path}
        if popen_factory is not None:
            client_options["popen_factory"] = popen_factory
        client = AdbClient(profile.serial, **client_options)

    lines: list[str] = []
    reader_error: list[BaseException] = []
    reader: threading.Thread | None = None
    try:
        try:
            with client.getevent_lines(profile.touch_device) as event_lines:

                def read_stream() -> None:
                    try:
                        lines.extend(event_lines)
                    except (OSError, ValueError) as error:
                        reader_error.append(error)

                reader = threading.Thread(
                    target=read_stream,
                    name="coc-farm2-getevent",
                    daemon=True,
                )
                reader.start()
                wait_for_stop()
        except AdbError as error:
            raise CaptureError(
                f"could not capture touchscreen events: {error}"
            ) from error
    finally:
        if reader is not None:
            reader.join(timeout=2)

    if reader is not None and reader.is_alive():
        raise CaptureError("touch capture did not stop cleanly")
    if reader_error:
        raise CaptureError(f"could not read touchscreen events: {reader_error[0]}")
    trace = "".join(lines)
    if not trace.strip():
        raise CaptureError(
            "no touchscreen events were captured; perform gestures "
            "before pressing Enter"
        )
    return trace
