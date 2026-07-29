"""Unit tests for persistent adb shell input session."""

from __future__ import annotations

from coc_farm2.adb import AdbClient


def test_adb_client_tap_uses_persistent_shell() -> None:
    commands: list[str] = []

    class _Shell:
        alive = True

        def run(self, command: str, *, timeout_s: float | None = None) -> None:
            commands.append(command)

        def close(self) -> None:
            return None

    client = AdbClient("SERIAL")
    client._input_shell = _Shell()  # type: ignore[assignment]
    client.tap(10, 20, duration_ms=50)
    assert len(commands) == 1
    assert commands[0] == "input swipe 10 20 10 20 50"
    client.close_input_shell()


def test_adb_client_swipe_two_point_uses_input_swipe() -> None:
    commands: list[str] = []

    class _Shell:
        alive = True

        def run(self, command: str, *, timeout_s: float | None = None) -> None:
            commands.append(command)

        def close(self) -> None:
            return None

    client = AdbClient("SERIAL")
    client._input_shell = _Shell()  # type: ignore[assignment]
    client.swipe(1, 2, 3, 4, duration_ms=100)
    assert commands[0] == "input swipe 1 2 3 4 100"


def test_adb_client_multipoint_uses_gesture_helper() -> None:
    commands: list[str] = []

    class _Shell:
        alive = True

        def run(self, command: str, *, timeout_s: float | None = None) -> None:
            commands.append(command)

        def close(self) -> None:
            return None

    client = AdbClient("SERIAL")
    client._gesture_helper_installed = True
    client._input_shell = _Shell()  # type: ignore[assignment]
    client.swipe(
        0,
        0,
        10,
        10,
        duration_ms=100,
        points=((0, 0), (5, 5), (10, 10)),
        times_ms=(0, 50, 100),
    )
    assert "app_process" in commands[0]
    assert " path " in commands[0]
