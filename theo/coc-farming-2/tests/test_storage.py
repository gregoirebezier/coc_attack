from pathlib import Path

from coc_farm2.models import (
    AppBounds,
    ContactGroupAction,
    ContactSample,
    DeviceProfile,
    FarmConfig,
    LootThresholds,
    Macro,
    Point,
)
from coc_farm2.storage import ProjectStore


def _profile() -> DeviceProfile:
    return DeviceProfile(
        serial="X",
        model="M",
        android_api=34,
        package="p",
        activity="a",
        app_version="1",
        logical_width=10,
        logical_height=10,
        raw_width=10,
        raw_height=10,
        rotation=0,
        touch_device="/dev/input/event0",
        app_bounds=AppBounds(0, 0, 10, 10),
    )


def _tap(x: int = 1, y: int = 1) -> ContactGroupAction:
    return ContactGroupAction(
        delay_ms=0,
        samples=(
            ContactSample(0, 0, x, y, "down"),
            ContactSample(20, 0, x, y, "up"),
        ),
    )


def test_config_and_attack_macros(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    store.save_profile(_profile())
    store.save_config(
        FarmConfig(
            thresholds=LootThresholds(gold=1, elixir=2, dark=0),
            next_button=Point(1, 2),
        )
    )
    loaded = store.load_config()
    assert loaded.thresholds.gold == 1
    assert loaded.next_button == Point(1, 2)

    macro = Macro(
        name="attack-01",
        profile=_profile(),
        actions=(_tap(),),
        approved=False,
        source_take_name="01",
    )
    store.save_attack_macro("01", macro)
    assert len(store.load_attack_macros()) == 1
    store.approve_attack("01")
    assert len(store.load_approved_attack_macros()) == 1


def test_delete_attack_removes_artifacts(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    profile = _profile()
    macro = Macro(
        name="attack-01",
        profile=profile,
        actions=(_tap(),),
        source_take_name="01",
    )
    store.save_attack_macro("01", macro)
    take_path = store.root / "takes" / "attack" / "01.json"
    take_path.parent.mkdir(parents=True, exist_ok=True)
    take_path.write_text("{}", encoding="utf-8")
    (store.root / "takes" / "attack" / "01.getevent.txt").write_text(
        "trace", encoding="utf-8"
    )
    store.previews_dir.mkdir(parents=True, exist_ok=True)
    (store.previews_dir / "attack-01.png").write_bytes(b"png")

    removed = store.delete_attack("01")
    assert len(removed) >= 3
    assert not (store.root / "macros" / "attacks" / "01.json").exists()
    assert not take_path.exists()
    assert store.next_take_number("attack") == 1
