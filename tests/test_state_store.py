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


def test_websocket_delta_is_copy_on_write_and_advances_only_present_fields():
    store = seed_store(device())
    before = store.data
    before_state = dict(before["states"]["1"])

    update = store.apply_websocket_delta(
        1,
        state_delta={"pwr_on": False},
        device_delta={"condition": {"pwr_on": 0}},
    )
    assert update.changed is True
    assert update.data is store.data
    assert update.data is not before
    assert before["states"]["1"] == before_state
    assert update.data["states"]["1"]["pwr_on"] is False
    revisions = store.capture_device("1").revisions
    assert revisions["state.pwr_on"] > 0
    assert revisions["device.condition.pwr_on"] == revisions["state.pwr_on"]
    assert "state.fan_speed" not in revisions


def test_unchanged_websocket_observation_advances_revision_without_publishing():
    store = seed_store(device(pwr_on=1))
    before = store.data
    baseline = store.capture_device("1")

    update = store.apply_websocket_delta("1", state_delta={"pwr_on": True})

    assert update == StateStoreUpdate(before, False)
    assert store.data is before
    assert (
        store.capture_device("1").revisions["state.pwr_on"]
        > baseline.revisions.get("state.pwr_on", 0)
    )


def test_websocket_none_is_distinct_from_a_missing_field():
    store = seed_store(device())

    update = store.apply_websocket_delta(
        "1",
        state_delta={"optional": None},
        device_delta={"condition": {"optional": None}},
    )

    assert update.changed is True
    assert "optional" in update.data["states"]["1"]
    assert update.data["states"]["1"]["optional"] is None
    assert "optional" in update.data["device_map"]["1"].condition
    assert update.data["device_map"]["1"].condition["optional"] is None
    revisions = store.capture_device("1").revisions
    assert revisions["state.optional"] == revisions["device.condition.optional"]


def test_websocket_delta_owns_mutable_values_and_preserves_device_identity():
    store = seed_store(device())
    before = store.data
    state_history = {"samples": [18]}
    device_history = {"samples": [19]}

    update = store.apply_websocket_delta(
        "1",
        state_delta={"history": state_history},
        device_delta={
            "id": 999,
            "condition": {"history": device_history},
        },
    )
    state_history["samples"].append(20)
    device_history["samples"].append(21)

    assert update.data["states"]["1"]["history"] == {"samples": [18]}
    assert update.data["device_map"]["1"].condition["history"] == {
        "samples": [19]
    }
    assert update.data["device_map"]["1"].id == 1
    assert set(update.data["device_map"]) == {"1"}
    assert "device.id" not in store.capture_device("1").revisions
    assert "history" not in before["states"]["1"]
    assert "history" not in before["device_map"]["1"].condition


def test_websocket_delta_for_unknown_device_is_an_unchanged_noop():
    store = seed_store(device())
    before = store.data

    update = store.apply_websocket_delta(
        "999",
        state_delta={"pwr_on": False},
        device_delta={"condition": {"pwr_on": 0}},
    )

    assert update == StateStoreUpdate(before, False)
    assert store.data is before
    assert set(store.capture_all()) == {"1"}


def test_websocket_state_id_delta_is_ignored():
    store = seed_store(device())
    before = store.data

    update = store.apply_websocket_delta("1", state_delta={"id": 999})

    assert update == StateStoreUpdate(before, False)
    assert "id" not in store.data["states"]["1"]
    assert store.data["device_map"]["1"].id == 1
    assert set(store.data["states"]) == {"1"}
    assert set(store.data["device_map"]) == {"1"}
    assert "state.id" not in store.capture_device("1").revisions


def test_websocket_device_delta_preserves_dotted_nested_key():
    store = seed_store(device())

    update = store.apply_websocket_delta(
        "1",
        state_delta={},
        device_delta={"condition": {"a.b": 2}},
    )

    assert update.changed is True
    assert update.data["device_map"]["1"].condition["a.b"] == 2
    assert "a" not in update.data["device_map"]["1"].condition
    assert "device.condition.a.b" in store.capture_device("1").revisions


def test_websocket_device_delta_preserves_dotted_top_level_key():
    store = seed_store(device())

    update = store.apply_websocket_delta(
        "1",
        state_delta={},
        device_delta={"capability.version": 2},
    )

    assert update.changed is True
    assert update.data["device_map"]["1"].raw["capability.version"] == 2
    assert "capability" not in update.data["device_map"]["1"].raw
    assert "device.capability.version" in store.capture_device("1").revisions


def test_websocket_state_copy_on_write_shares_unaffected_device_state():
    store = seed_store(device(1), device(2, name="Second"))
    before = store.data
    before_target = before["states"]["1"]
    before_unaffected = before["states"]["2"]

    update = store.apply_websocket_delta(
        "1",
        state_delta={"pwr_on": False},
    )

    assert update.changed is True
    assert update.data["states"]["1"] is not before_target
    assert before_target["pwr_on"] is True
    assert update.data["states"]["1"]["pwr_on"] is False
    assert update.data["states"]["2"] is before_unaffected


def test_refresh_preserves_newer_websocket_fields_but_accepts_unrelated_fields():
    store = seed_store(device(name="Original", fan_speed=1, temp_in=170))
    baseline = store.capture_device("1")
    store.apply_websocket_delta(
        "1",
        state_delta={"fan_speed": 7},
        device_delta={"name": "Push Name", "condition": {"fan_speed": 6}},
    )
    result = store.apply_refresh(
        device(name="Stale Name", fan_speed=1, temp_in=225),
        baseline,
    )
    assert result.changed is True
    assert result.data["states"]["1"]["fan_speed"] == 7
    assert result.data["states"]["1"]["temp_in"] == 225
    assert result.data["device_map"]["1"].name == "Push Name"
    assert result.data["device_map"]["1"].condition["temp_in"] == 225


def test_same_value_newer_push_still_blocks_an_older_different_refresh():
    store = seed_store(device(pwr_on=1))
    baseline = store.capture_device("1")
    store.apply_websocket_delta("1", state_delta={"pwr_on": True})

    result = store.apply_refresh(device(pwr_on=0), baseline)

    assert result.data["states"]["1"]["pwr_on"] is True


def test_refresh_baseline_device_mismatch_is_atomic():
    store = seed_store(device())
    store.apply_websocket_delta("1", state_delta={"pwr_on": True})
    store._absence_counts["1"] = 1
    before = store.data
    before_revisions = dict(store.capture_device("1").revisions)
    mismatch = FieldRevisionBaseline("2", MappingProxyType({}))

    with pytest.raises(ValueError, match="baseline device id"):
        store.apply_refresh(device(1, name="Rejected"), mismatch)

    assert store.data is before
    assert store.capture_device("1").revisions == before_revisions
    assert store._absence_counts == {"1": 1}


def test_refresh_adds_an_owned_clone_for_an_unknown_device():
    store = AtmeexStateStore()
    incoming = device("0007")
    incoming.raw["condition"]["history"] = {"samples": [18]}
    baseline = store.capture_device("7")

    update = store.apply_refresh(incoming, baseline)
    stored = update.data["device_map"]["7"]
    incoming.raw["condition"]["history"]["samples"].append(19)

    assert update.changed is True
    assert stored is not incoming
    assert stored.id == 7
    assert set(update.data["device_map"]) == {"7"}
    assert set(update.data["states"]) == {"7"}
    assert stored.condition["history"] == {"samples": [18]}
    assert "device.id" not in store.capture_device("7").revisions


def test_unchanged_refresh_advances_accepted_revisions_without_publishing():
    store = seed_store(device())
    store._absence_counts["1"] = 1
    before = store.data
    baseline = store.capture_device("1")

    update = store.apply_refresh(device(), baseline)

    assert update == StateStoreUpdate(before, False)
    revisions = store.capture_device("1").revisions
    assert revisions["device.name"] > baseline.revisions.get("device.name", 0)
    assert revisions["state.pwr_on"] > baseline.revisions.get("state.pwr_on", 0)
    assert "device.id" not in revisions
    assert "state.id" not in revisions
    assert store._absence_counts == {"1": 1}


def test_refresh_copies_only_the_changed_target_state():
    store = seed_store(
        device(1, temp_in=170),
        device(2, name="Second", temp_in=180),
    )
    before = store.data
    before_target = before["states"]["1"]
    before_unaffected = before["states"]["2"]
    baseline = store.capture_device("1")

    update = store.apply_refresh(device(1, temp_in=225), baseline)

    assert before_target["temp_in"] == 170
    assert update.data["states"]["1"] is not before_target
    assert update.data["states"]["1"]["temp_in"] == 225
    assert update.data["states"]["2"] is before_unaffected
