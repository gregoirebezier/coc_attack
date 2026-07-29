"""Filesystem layout for runtime artifacts under ``.coc-farm2/``."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from coc_farm2.models import (
    SCHEMA_VERSION,
    DeviceProfile,
    FarmConfig,
    Macro,
    OcrRegion,
    PixelProbe,
    RecordedTake,
)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class StorageError(RuntimeError):
    """Raised when a runtime artifact cannot be stored safely."""


class ProjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def default(cls) -> ProjectStore:
        return cls(Path.cwd() / ".coc-farm2")

    @property
    def profile_path(self) -> Path:
        return self.root / "device.json"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def probes_path(self) -> Path:
        return self.root / "probes.json"

    @property
    def ocr_regions_path(self) -> Path:
        return self.root / "ocr_regions.json"

    @property
    def previews_dir(self) -> Path:
        return self.root / "previews"

    @property
    def calibration_dir(self) -> Path:
        return self.root / "calibration"

    @property
    def helper_dir(self) -> Path:
        return self.root / "helper"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def lock_path(self) -> Path:
        return self.root / "runner.lock"

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def save_profile(self, profile: DeviceProfile) -> Path:
        return self._write_json(self.profile_path, profile.to_dict())

    def load_profile(self) -> DeviceProfile:
        return DeviceProfile.from_dict(self._read_json(self.profile_path))

    def profile_exists(self) -> bool:
        return self.profile_path.is_file()

    def save_config(self, config: FarmConfig) -> Path:
        return self._write_json(self.config_path, config.to_dict())

    def load_config(self) -> FarmConfig:
        if not self.config_path.is_file():
            config = FarmConfig()
            self.save_config(config)
            return config
        return FarmConfig.from_dict(self._read_json(self.config_path))

    def save_probes(self, probes: dict[str, PixelProbe]) -> Path:
        for name, probe in probes.items():
            if name != probe.name:
                raise StorageError(
                    f"probe map key {name!r} does not match probe name {probe.name!r}"
                )
        document = {
            "schema_version": SCHEMA_VERSION,
            "probes": {name: probe.to_dict() for name, probe in sorted(probes.items())},
        }
        return self._write_json(self.probes_path, document)

    def load_probes(self) -> dict[str, PixelProbe]:
        if not self.probes_path.is_file():
            return {}
        document = self._read_json(self.probes_path)
        version = int(document.get("schema_version", 0))
        if version != SCHEMA_VERSION:
            raise StorageError(
                f"unsupported probes schema version {version}; "
                f"expected {SCHEMA_VERSION}"
            )
        raw = document.get("probes", {})
        if not isinstance(raw, dict):
            raise StorageError("probes document is invalid")
        return {str(name): PixelProbe.from_dict(value) for name, value in raw.items()}

    def save_ocr_regions(self, regions: dict[str, OcrRegion]) -> Path:
        for name, region in regions.items():
            if name != region.name:
                raise StorageError(
                    f"OCR map key {name!r} does not match region name {region.name!r}"
                )
        document = {
            "schema_version": SCHEMA_VERSION,
            "regions": {
                name: region.to_dict() for name, region in sorted(regions.items())
            },
        }
        return self._write_json(self.ocr_regions_path, document)

    def load_ocr_regions(self) -> dict[str, OcrRegion]:
        if not self.ocr_regions_path.is_file():
            return {}
        document = self._read_json(self.ocr_regions_path)
        version = int(document.get("schema_version", 0))
        if version != SCHEMA_VERSION:
            raise StorageError(
                f"unsupported OCR schema version {version}; expected {SCHEMA_VERSION}"
            )
        raw = document.get("regions", {})
        if not isinstance(raw, dict):
            raise StorageError("OCR regions document is invalid")
        return {str(name): OcrRegion.from_dict(value) for name, value in raw.items()}

    def save_take(
        self,
        macro_name: str,
        take_number: int,
        take: RecordedTake,
    ) -> Path:
        safe_name = self._safe_name(macro_name)
        if take_number <= 0:
            raise StorageError("take number must be positive")
        path = self.root / "takes" / safe_name / f"{take_number:02d}.json"
        return self._write_json(path, take.to_dict())

    def take_trace_path(self, macro_name: str, take_number: int) -> Path:
        safe_name = self._safe_name(macro_name)
        if take_number <= 0:
            raise StorageError("take number must be positive")
        return self.root / "takes" / safe_name / f"{take_number:02d}.getevent.txt"

    def next_take_number(self, macro_name: str) -> int:
        safe_name = self._safe_name(macro_name)
        directory = self.root / "takes" / safe_name
        if not directory.exists():
            return 1
        numbers: list[int] = []
        for path in directory.glob("*.json"):
            try:
                numbers.append(int(path.stem))
            except ValueError:
                continue
        return (max(numbers) + 1) if numbers else 1

    def load_takes(self, macro_name: str) -> list[RecordedTake]:
        safe_name = self._safe_name(macro_name)
        directory = self.root / "takes" / safe_name
        if not directory.exists():
            return []
        return [
            RecordedTake.from_dict(self._read_json(path))
            for path in sorted(directory.glob("*.json"))
        ]

    def save_macro(self, macro: Macro) -> Path:
        safe_name = self._safe_name(macro.name)
        if macro.name.startswith("attack/") or safe_name.startswith("attack"):
            # attacks live under macros/attacks/
            pass
        if macro.name.startswith("attack-") or macro.name.startswith("attack_"):
            path = self.root / "macros" / "attacks" / f"{safe_name}.json"
        elif macro.name == "start_search":
            path = self.root / "macros" / "start_search.json"
        else:
            path = self.root / "macros" / f"{safe_name}.json"
        return self._write_json(path, macro.to_dict())

    def save_attack_macro(self, take_id: str, macro: Macro) -> Path:
        safe_id = self._safe_name(take_id)
        path = self.root / "macros" / "attacks" / f"{safe_id}.json"
        return self._write_json(path, macro.to_dict())

    def load_macro(self, macro_name: str) -> Macro:
        if macro_name == "start_search":
            path = self.root / "macros" / "start_search.json"
        else:
            path = self.root / "macros" / f"{self._safe_name(macro_name)}.json"
        return Macro.from_dict(self._read_json(path))

    def load_attack_macros(self) -> list[Macro]:
        directory = self.root / "macros" / "attacks"
        if not directory.exists():
            return []
        macros = [
            Macro.from_dict(self._read_json(path))
            for path in sorted(directory.glob("*.json"))
        ]
        return macros

    def load_approved_attack_macros(self) -> list[Macro]:
        return [macro for macro in self.load_attack_macros() if macro.approved]

    def approve_attack(self, take_id: str) -> Macro:
        path = self.root / "macros" / "attacks" / f"{self._safe_name(take_id)}.json"
        macro = Macro.from_dict(self._read_json(path))
        approved = replace(macro, approved=True)
        self._write_json(path, approved.to_dict())
        return approved

    def approve_macro(self, macro_name: str) -> Macro:
        macro = self.load_macro(macro_name)
        approved = replace(macro, approved=True)
        self.save_macro(approved)
        return approved

    def macro_exists(self, macro_name: str) -> bool:
        if macro_name == "start_search":
            return (self.root / "macros" / "start_search.json").is_file()
        return (self.root / "macros" / f"{self._safe_name(macro_name)}.json").is_file()

    def delete_attack(self, take_id: str) -> list[Path]:
        """Remove macro, take, raw getevent, and preview for one attack template."""
        safe_id = self._safe_name(take_id)
        candidates = [
            self.root / "macros" / "attacks" / f"{safe_id}.json",
            self.root / "takes" / "attack" / f"{safe_id}.json",
            self.root / "takes" / "attack" / f"{safe_id}.getevent.txt",
            self.root / "takes" / "attack" / f"{safe_id}.getevent.failed.txt",
            self.root / "previews" / f"attack-{safe_id}.png",
            self.root / "previews" / f"{safe_id}.png",
        ]
        removed = self._unlink_existing(candidates)
        if not removed:
            raise StorageError(f"no attack template artifacts found for {take_id!r}")
        return removed

    def delete_start_search(self) -> list[Path]:
        """Remove the start_search macro, takes, and preview."""
        candidates = [
            self.root / "macros" / "start_search.json",
            self.root / "previews" / "start_search.png",
        ]
        takes_dir = self.root / "takes" / "start_search"
        if takes_dir.is_dir():
            candidates.extend(sorted(takes_dir.iterdir()))
        removed = self._unlink_existing(candidates)
        if not removed:
            raise StorageError("no start_search artifacts found")
        return removed

    def _unlink_existing(self, paths: list[Path]) -> list[Path]:
        removed: list[Path] = []
        for path in paths:
            if not path.is_file():
                continue
            try:
                path.unlink()
            except OSError as error:
                raise StorageError(f"could not delete {path}: {error}") from error
            removed.append(path)
        return removed

    def _safe_name(self, value: str) -> str:
        if not _SAFE_NAME.fullmatch(value):
            raise StorageError(
                "artifact must use a safe name containing only letters, "
                "numbers, dots, underscores, or hyphens"
            )
        return value

    def _write_json(self, path: Path, value: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise StorageError(f"could not write {path}: {error}") from error
        return path

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise StorageError(f"missing runtime artifact: {path}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise StorageError(f"could not read {path}: {error}") from error
        if not isinstance(value, dict):
            raise StorageError(f"expected a JSON object in {path}")
        return value
