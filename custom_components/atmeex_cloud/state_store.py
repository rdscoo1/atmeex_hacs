from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .api import AtmeexDevice, AtmeexState
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


def _flatten_device(raw: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in raw.items():
        if key in ("condition", "settings") and isinstance(value, Mapping):
            for nested_key, nested_value in value.items():
                fields[f"device.{key}.{nested_key}"] = nested_value
        else:
            fields[f"device.{key}"] = value
    return fields


def _rebuild_device(
    base: AtmeexDevice, fields: Mapping[str, Any]
) -> AtmeexDevice:
    raw = deepcopy(base.to_ha_dict())
    raw["condition"] = dict(raw.get("condition", {}))
    raw["settings"] = dict(raw.get("settings", {}))
    for path, value in fields.items():
        field_path = path.removeprefix("device.")
        section, separator, nested_field = field_path.partition(".")
        if separator and section in ("condition", "settings"):
            raw[section][nested_field] = deepcopy(value)
        else:
            raw[field_path] = deepcopy(value)
    raw["id"] = base.id
    return AtmeexDevice.from_raw(raw)


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

    def _commit(
        self,
        device_map: dict[str, AtmeexDevice],
        states: dict[str, dict[str, Any]],
        changed_paths: set[tuple[str, str]],
        touched_paths: set[tuple[str, str]] | None = None,
        removed: frozenset[str] = frozenset(),
    ) -> StateStoreUpdate:
        revision_paths = changed_paths if touched_paths is None else touched_paths
        if revision_paths:
            self._revision += 1
            for device_id, path in revision_paths:
                self._field_revisions.setdefault(device_id, {})[path] = self._revision
        if not changed_paths and not removed:
            return StateStoreUpdate(self._data, False)
        for device_id in removed:
            self._field_revisions.pop(device_id, None)
            self._absence_counts.pop(device_id, None)
        self._data = {
            "devices": [deepcopy(item.to_ha_dict()) for item in device_map.values()],
            "device_map": dict(device_map),
            "states": dict(states),
        }
        return StateStoreUpdate(self._data, True, removed)

    def apply_websocket_delta(
        self,
        device_id: int | str,
        *,
        state_delta: Mapping[str, Any],
        device_delta: Mapping[str, Any] | None = None,
    ) -> StateStoreUpdate:
        key = normalize_device_id(device_id)
        current_map = self._data.get("device_map", {})
        current_device = current_map.get(key)
        if current_device is None:
            return StateStoreUpdate(self._data, False)
        device_map = dict(current_map)
        states = dict(self._data.get("states", {}))
        changed: set[tuple[str, str]] = set()
        touched: set[tuple[str, str]] = set()

        state_updates = [
            (field, value) for field, value in state_delta.items() if field != "id"
        ]
        if state_updates:
            state = deepcopy(states.get(key, {}))
            states[key] = state
            for field, value in state_updates:
                path = f"state.{field}"
                touched.add((key, path))
                if field not in state or state[field] != value:
                    state[field] = deepcopy(value)
                    changed.add((key, path))

        if device_delta:
            current_fields = _flatten_device(current_device.to_ha_dict())
            accepted = _flatten_device(device_delta)
            replacements: dict[str, Any] = {}
            for path, value in accepted.items():
                if path == "device.id":
                    continue
                touched.add((key, path))
                if path not in current_fields or current_fields[path] != value:
                    replacements[path] = deepcopy(value)
                    changed.add((key, path))
            if replacements:
                device_map[key] = _rebuild_device(current_device, replacements)

        return self._commit(
            device_map,
            states,
            changed,
            touched_paths=touched,
        )

    def _merge_device(
        self,
        incoming: AtmeexDevice,
        baseline: FieldRevisionBaseline,
        device_map: dict[str, AtmeexDevice],
        states: dict[str, dict[str, Any]],
    ) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        key = normalize_device_id(incoming.id)
        if baseline.device_id != key:
            raise ValueError("baseline device id does not match response device id")

        changed: set[tuple[str, str]] = set()
        touched: set[tuple[str, str]] = set()
        current_device = device_map.get(key)
        current_fields = (
            {}
            if current_device is None
            else _flatten_device(current_device.to_ha_dict())
        )
        incoming_fields = _flatten_device(incoming.to_ha_dict())
        incoming_fields.pop("device.id", None)
        replacements: dict[str, Any] = {}
        revisions = self._field_revisions.get(key, {})

        for path, value in incoming_fields.items():
            if revisions.get(path, 0) != baseline.revisions.get(path, 0):
                continue
            touched.add((key, path))
            if path not in current_fields or current_fields[path] != value:
                replacements[path] = deepcopy(value)
                changed.add((key, path))

        if current_device is not None and replacements:
            device_map[key] = _rebuild_device(current_device, replacements)
        elif current_device is None:
            accepted_device_paths = {
                path for item_key, path in touched if item_key == key
            }
            if accepted_device_paths != set(incoming_fields):
                return set(), set()
            device_map[key] = AtmeexDevice.from_raw(
                deepcopy(incoming.to_ha_dict())
            )

        incoming_state = AtmeexState.from_device_dict(
            incoming.to_ha_dict()
        ).to_ha_dict()
        current_state = states.get(key, {})
        state_replacements: dict[str, Any] = {}
        for field, value in incoming_state.items():
            if field == "id":
                continue
            path = f"state.{field}"
            if revisions.get(path, 0) != baseline.revisions.get(path, 0):
                continue
            touched.add((key, path))
            if field not in current_state or current_state[field] != value:
                state_replacements[field] = deepcopy(value)
                changed.add((key, path))
        if state_replacements:
            merged_state = deepcopy(current_state)
            merged_state.update(state_replacements)
            states[key] = merged_state

        return changed, touched

    def apply_refresh(
        self,
        device: AtmeexDevice,
        baseline: FieldRevisionBaseline,
    ) -> StateStoreUpdate:
        device_map = dict(self._data.get("device_map", {}))
        states = dict(self._data.get("states", {}))
        changed, touched = self._merge_device(
            device,
            baseline,
            device_map,
            states,
        )
        return self._commit(
            device_map,
            states,
            changed,
            touched_paths=touched,
        )
