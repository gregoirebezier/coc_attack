from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

from coc_farm2.adb import SafetyStatus
from coc_farm2.models import (
    AppBounds,
    ContactGroupAction,
    ContactSample,
    DeviceProfile,
    Macro,
    WaitAction,
)
from coc_farm2.replay import macro_needs_helper, replay_macro


def _profile() -> DeviceProfile:
    return DeviceProfile(
        serial="X",
        model="M",
        android_api=34,
        package="p",
        activity="a",
        app_version="1",
        logical_width=200,
        logical_height=100,
        raw_width=100,
        raw_height=200,
        rotation=0,
        touch_device="/dev/input/event0",
        app_bounds=AppBounds(0, 0, 200, 100),
    )


def _group(x: int, y: int) -> ContactGroupAction:
    return ContactGroupAction(
        delay_ms=0,
        samples=(
            ContactSample(0, 0, x, y, "down"),
            ContactSample(20, 0, x, y, "up"),
        ),
    )


@dataclass
class FakeDevice:
    profile: DeviceProfile
    sessions: list[tuple[ContactGroupAction, ...]] = field(default_factory=list)

    def safety_status(self, expected: DeviceProfile) -> SafetyStatus:
        return SafetyStatus(
            online=True,
            foreground=True,
            unlocked=True,
            logical_width=expected.logical_width,
            logical_height=expected.logical_height,
            rotation=expected.rotation,
            app_bounds=expected.app_bounds,
            app_version=expected.app_version,
        )

    def tap(self, x: int, y: int, duration_ms: int = 0) -> None:
        return None

    def inject_contacts(
        self, action: ContactGroupAction, profile: DeviceProfile
    ) -> None:
        self.inject_contact_session((action,), profile)

    def inject_contact_session(
        self,
        groups: tuple[ContactGroupAction, ...] | list[ContactGroupAction],
        profile: DeviceProfile,
    ) -> None:
        self.sessions.append(tuple(groups))

    def screenshot(self) -> Image.Image:
        return Image.new("RGB", (200, 100), color=(0, 0, 0))


def test_replay_batches_contacts_into_one_session() -> None:
    profile = _profile()
    device = FakeDevice(profile=profile)
    macro = Macro(
        name="t",
        profile=profile,
        actions=(
            _group(10, 20),
            _group(11, 21),
            WaitAction(delay_ms=0, duration_ms=50),
            _group(30, 40),
        ),
    )
    sleeps: list[float] = []
    replay_macro(
        device,
        macro,
        sleeper=lambda s: sleeps.append(s),
    )
    # Two contact runs split by wait → two sessions (2 groups, then 1).
    assert len(device.sessions) == 2
    assert len(device.sessions[0]) == 2
    assert len(device.sessions[1]) == 1
    assert device.sessions[0][0].samples[0].x == 10
    assert device.sessions[1][0].samples[0].x == 30
    assert 0.05 in sleeps


def test_macro_needs_helper() -> None:
    profile = _profile()
    plain = Macro(
        name="a",
        profile=profile,
        actions=(WaitAction(0, 10),),
    )
    with_contacts = Macro(
        name="b",
        profile=profile,
        actions=(_group(1, 1),),
    )
    assert not macro_needs_helper(plain)
    assert macro_needs_helper(with_contacts)
