"""Contact session flattening for single-JVM replay."""

from coc_farm2.models import ContactGroupAction, ContactSample
from coc_farm2.session import flatten_contact_session, session_duration_ms


def _group(delay_ms: int, hold_ms: int, x: int, y: int) -> ContactGroupAction:
    return ContactGroupAction(
        delay_ms=delay_ms,
        samples=(
            ContactSample(0, 0, x, y, "down"),
            ContactSample(hold_ms, 0, x, y, "up"),
        ),
    )


def test_flatten_puts_gaps_on_absolute_clock() -> None:
    groups = (
        _group(0, 100, 10, 10),
        _group(500, 50, 20, 20),
    )
    events = flatten_contact_session(groups)
    assert events[0] == (0, 0, 10, 10, "down")
    assert events[1] == (100, 0, 10, 10, "up")
    assert events[2] == (600, 0, 20, 20, "down")  # 100 + 500
    assert events[3] == (650, 0, 20, 20, "up")
    assert session_duration_ms(groups) == 650


def test_session_inject_uses_one_app_process() -> None:
    from contextlib import nullcontext

    from coc_farm2.adb import AdbClient
    from coc_farm2.models import AppBounds, DeviceProfile

    pushes: list[list[str]] = []
    commands: list[str] = []

    class _Shell:
        alive = True

        def run(self, command: str, *, timeout_s: float | None = None) -> None:
            commands.append(command)

        def close(self) -> None:
            return None

    profile = DeviceProfile(
        serial="SERIAL",
        model="M",
        android_api=34,
        package="p",
        activity="a",
        app_version="1",
        logical_width=100,
        logical_height=100,
        raw_width=100,
        raw_height=100,
        rotation=0,
        touch_device="/dev/input/event0",
        app_bounds=AppBounds(0, 0, 100, 100),
    )
    client = AdbClient("SERIAL")
    client._gesture_helper_installed = True
    client._input_shell = _Shell()  # type: ignore[assignment]
    client._without_input_shell = lambda: nullcontext()  # type: ignore[method-assign]

    def _run_text(command: list[str], **kwargs: object) -> str:
        pushes.append(list(command))
        return ""

    client._run_text = _run_text  # type: ignore[method-assign]
    client.inject_contact_session(
        (_group(0, 40, 1, 2), _group(200, 40, 3, 4)),
        profile,
    )
    assert any("push" in c for c in pushes)
    assert len(commands) == 1
    assert " session " in commands[0]
    assert "app_process" in commands[0]
