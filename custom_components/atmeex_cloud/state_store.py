from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from .api import AtmeexDevice
from .helpers import normalize_device_id

if TYPE_CHECKING:
    from .coordinator import AtmeexCoordinatorData


@dataclass(frozen=True, slots=True)
class FieldRevisionBaseline:
    device_id: str
    revisions: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class StateStoreUpdate:
    data: AtmeexCoordinatorData
    changed: bool
    removed_device_ids: frozenset[str] = frozenset()


def _empty_data() -> AtmeexCoordinatorData:
    return {"devices": [], "device_map": {}, "states": {}}


class AtmeexStateStore:
    """Canonical copy-on-write device snapshot with per-field revisions."""

    def __init__(self, initial: AtmeexCoordinatorData | None = None) -> None:
        source = initial or _empty_data()
        self._data: AtmeexCoordinatorData = {
            "devices": [deepcopy(item) for item in source.get("devices", [])],
            "device_map": {
                normalize_device_id(device_id): AtmeexDevice.from_raw(
                    deepcopy(device.to_ha_dict())
                )
                for device_id, device in source.get("device_map", {}).items()
            },
            "states": {
                normalize_device_id(device_id): deepcopy(state)
                for device_id, state in source.get("states", {}).items()
            },
        }
        self._revision = 0
        self._field_revisions: dict[str, dict[str, int]] = {}
        self._absence_counts: dict[str, int] = {}

    @property
    def data(self) -> AtmeexCoordinatorData:
        return self._data

    def capture_device(self, device_id: int | str) -> FieldRevisionBaseline:
        key = normalize_device_id(device_id)
        return FieldRevisionBaseline(
            key,
            MappingProxyType(dict(self._field_revisions.get(key, {}))),
        )

    def capture_all(self) -> dict[str, FieldRevisionBaseline]:
        keys = set(self._data.get("device_map", {})) | set(self._field_revisions)
        return {key: self.capture_device(key) for key in keys}
