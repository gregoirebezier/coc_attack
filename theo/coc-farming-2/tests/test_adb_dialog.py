"""System-dialog detection around the full-screen game surface."""

from __future__ import annotations

from coc_farm2.adb import AdbClient


def test_dismiss_foreign_dialog_presses_back_and_returns_visible_text() -> None:
    commands: list[str] = []
    client = AdbClient("SERIAL")
    client.ui_hierarchy = lambda: (  # type: ignore[method-assign]
        '<hierarchy><node package="com.android.phone" '
        'text="Mise à jour de l&apos;opérateur" /></hierarchy>'
    )

    def input_run(command: str, *, timeout_s: float | None = None) -> None:
        commands.append(command)

    client._input_run = input_run  # type: ignore[method-assign]

    label = client.dismiss_foreign_dialog("com.supercell.clashofclans")

    assert label == "Mise à jour de l'opérateur"
    assert commands == ["input keyevent KEYCODE_BACK"]


def test_dismiss_foreign_dialog_ignores_game_hierarchy() -> None:
    client = AdbClient("SERIAL")
    client.ui_hierarchy = lambda: (  # type: ignore[method-assign]
        '<hierarchy><node package="com.supercell.clashofclans" text="" /></hierarchy>'
    )

    assert client.dismiss_foreign_dialog("com.supercell.clashofclans") is None


def test_dismiss_foreign_dialog_force_stops_non_cancelable_cidmanager() -> None:
    commands: list[str] = []
    client = AdbClient("SERIAL")
    client.ui_hierarchy = lambda: (  # type: ignore[method-assign]
        '<hierarchy><node package="com.samsung.android.cidmanager" '
        'text="Mise à jour de l&apos;opérateur" /></hierarchy>'
    )

    def input_run(command: str, *, timeout_s: float | None = None) -> None:
        commands.append(command)

    client._input_run = input_run  # type: ignore[method-assign]

    label = client.dismiss_foreign_dialog("com.supercell.clashofclans")

    assert label == "Mise à jour de l'opérateur"
    assert commands == ["am force-stop com.samsung.android.cidmanager"]
