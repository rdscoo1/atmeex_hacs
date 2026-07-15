from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from custom_components.atmeex_cloud.api import AtmeexDevice, AtmeexState
from custom_components.atmeex_cloud.state_store import (
    AtmeexStateStore,
    FieldRevisionBaseline,
    StateStoreUpdate,
)


def device(
    device_id: int | str = 1,
    *,
    name: str = "Breezer",
    pwr_on: int = 1,
    fan_speed: int = 2,
    temp_in: int = 180,
) -> AtmeexDevice:
    return AtmeexDevice.from_raw(
        {
            "id": device_id,
            "name": name,
            "model": "Atmeex",
            "online": True,
            "condition": {
                "pwr_on": pwr_on,
                "fan_speed": fan_speed,
                "temp_in": temp_in,
            },
            "settings": {},
        }
    )


def seed_store(*devices: AtmeexDevice) -> AtmeexStateStore:
    device_map = {str(item.id): item for item in devices}
    return AtmeexStateStore(
        {
            "devices": [item.to_ha_dict() for item in devices],
            "device_map": device_map,
            "states": {
                key: AtmeexState.from_device_dict(item.to_ha_dict()).to_ha_dict()
                for key, item in device_map.items()
            },
        }
    )


def test_empty_store_contract_and_immutable_baseline():
    store = AtmeexStateStore()

    assert store.data == {"devices": [], "device_map": {}, "states": {}}
    baseline = store.capture_device(1)
    assert baseline == FieldRevisionBaseline("1", MappingProxyType({}))
    with pytest.raises(TypeError):
        baseline.revisions["state.pwr_on"] = 7
    assert store.capture_all() == {}


def test_constructor_canonicalizes_seeded_map_and_state_keys():
    seeded_device = device("0007")
    seeded_state = AtmeexState.from_device_dict(
        seeded_device.to_ha_dict()
    ).to_ha_dict()

    store = AtmeexStateStore(
        {
            "devices": [seeded_device.to_ha_dict()],
            "device_map": {"0007": seeded_device},
            "states": {"0007": seeded_state},
        }
    )

    assert set(store.data["device_map"]) == {"7"}
    assert set(store.data["states"]) == {"7"}
    assert store.data["states"]["7"] == seeded_state
    assert "0007" not in store.data["states"]
    assert set(store.capture_all()) == {"7"}


def test_constructor_owns_all_seeded_nested_data():
    device_row = {
        "id": 7,
        "name": "List device",
        "model": "Atmeex",
        "online": True,
        "condition": {"telemetry": {"value": "list-original"}},
        "settings": {"schedule": {"enabled": True}},
    }
    original_device = AtmeexDevice.from_raw(
        {
            "id": 7,
            "name": "Map device",
            "model": "Atmeex",
            "online": True,
            "condition": {"telemetry": {"value": "map-original"}},
            "settings": {"schedule": {"enabled": True}},
        }
    )
    state = {"id": 7, "history": {"temperatures": [18.0]}}

    store = AtmeexStateStore(
        {
            "devices": [device_row],
            "device_map": {"7": original_device},
            "states": {"7": state},
        }
    )
    stored_device = store.data["device_map"]["7"]

    assert stored_device is not original_device
    device_row["condition"]["telemetry"]["value"] = "list-mutated"
    device_row["settings"]["schedule"]["enabled"] = False
    state["history"]["temperatures"].append(19.0)
    original_device.raw["condition"]["telemetry"]["value"] = "map-mutated"
    original_device.raw["settings"]["schedule"]["enabled"] = False

    assert store.data["devices"][0]["condition"]["telemetry"]["value"] == (
        "list-original"
    )
    assert store.data["devices"][0]["settings"]["schedule"]["enabled"] is True
    assert store.data["states"]["7"]["history"]["temperatures"] == [18.0]
    assert stored_device.raw["condition"]["telemetry"]["value"] == "map-original"
    assert stored_device.raw["settings"]["schedule"]["enabled"] is True


def test_update_and_captured_revisions_are_immutable_snapshots():
    store = seed_store(device(7))
    store._field_revisions["7"] = {"state.pwr_on": 3}
    baseline = store.capture_device("7")
    update = StateStoreUpdate(store.data, False)

    assert update.removed_device_ids == frozenset()
    with pytest.raises(FrozenInstanceError):
        update.changed = True
    with pytest.raises(TypeError):
        baseline.revisions["state.pwr_on"] = 4

    store._field_revisions["7"]["state.pwr_on"] = 5
    assert baseline.revisions == {"state.pwr_on": 3}
