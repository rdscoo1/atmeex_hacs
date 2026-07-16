import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.atmeex_cloud import AtmeexRuntimeData
from custom_components.atmeex_cloud.select import (
    AtmeexHumidificationSelect,
    AtmeexBreezerSelect,
    HUM_OPTIONS,
    BREEZER_OPTIONS,
    async_setup_entry,
)
from custom_components.atmeex_cloud.api import ApiError, AtmeexDevice


def _make_selects(cond_overrides: dict | None = None):
    cond = {
        "hum_stg": 1,
        "damp_pos": 2,
    }
    if cond_overrides:
        cond.update(cond_overrides)

    coordinator = SimpleNamespace(
        data={"states": {"1": cond}},
        last_update_success=True,
        async_request_refresh=AsyncMock(),
    )

    api = MagicMock()
    api.set_humid_stage = AsyncMock()
    api.set_breezer_mode = AsyncMock()

    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )

    hum = AtmeexHumidificationSelect(coordinator, api, dev)
    breezer = AtmeexBreezerSelect(coordinator, api, dev)

    return hum, breezer, cond, api, coordinator


def test_humidification_select_current_option_from_hum_stg():
    hum, breezer, cond, api, coord = _make_selects({"hum_stg": 2})
    assert getattr(hum, "_attr_name", None) is None
    assert hum.current_option == HUM_OPTIONS[2]
    cond["hum_stg"] = 0
    assert hum.current_option == "off"


def test_humidification_select_unknown_state_ignores_stale_cache():
    hum, breezer, cond, api, coord = _make_selects({"hum_stg": "bad"})
    hum._attr_current_option = "2"

    assert hum.current_option is None

    cond.clear()
    assert hum.current_option is None

    cond["hum_stg"] = True
    assert hum.current_option is None


@pytest.mark.asyncio
async def test_humidification_select_async_select_option():
    hum, breezer, cond, api, coord = _make_selects({"hum_stg": 0})
    await hum.async_select_option("3")

    api.set_humid_stage.assert_awaited_once_with(1, 3)
    coord.async_request_refresh.assert_awaited_once()
    assert not hasattr(hum, "_attr_current_option")


@pytest.mark.asyncio
async def test_humidification_select_invalid_option_raises():
    hum, breezer, cond, api, coord = _make_selects()

    with pytest.raises(ServiceValidationError) as raised:
        await hum.async_select_option("invalid")

    assert raised.value.translation_key == "invalid_command_value"
    assert (
        raised.value.translation_placeholders["field"]
        == "humidification_option"
    )
    api.set_humid_stage.assert_not_awaited()
    coord.async_request_refresh.assert_not_awaited()



def test_breezer_select_current_option_from_damp_pos():
    hum, breezer, cond, api, coord = _make_selects(
        {"pwr_on": True, "damp_pos": 1}
    )
    assert getattr(breezer, "_attr_name", None) is None
    assert breezer.current_option == BREEZER_OPTIONS[1]

    cond["damp_pos"] = 10
    breezer._attr_current_option = BREEZER_OPTIONS[0]
    assert breezer.current_option is None

    cond.clear()
    assert breezer.current_option is None


@pytest.mark.asyncio
async def test_breezer_select_async_select_option():
    hum, breezer, cond, api, coord = _make_selects()
    await breezer.async_select_option(BREEZER_OPTIONS[3])

    api.set_breezer_mode.assert_awaited_once_with(1, 3)
    coord.async_request_refresh.assert_awaited_once()
    assert not hasattr(breezer, "_attr_current_option")


@pytest.mark.asyncio
async def test_breezer_select_invalid_option_raises():
    hum, breezer, cond, api, coord = _make_selects()

    with pytest.raises(ServiceValidationError) as raised:
        await breezer.async_select_option("неизвестно")

    assert raised.value.translation_key == "invalid_command_value"
    assert raised.value.translation_placeholders["field"] == "breezer_option"
    api.set_breezer_mode.assert_not_awaited()
    coord.async_request_refresh.assert_not_awaited()



@pytest.mark.asyncio
async def test_async_setup_entry_skips_hum_entities_until_capability_detected():
    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )
    listeners: list = []
    coordinator = SimpleNamespace(
        data={
            "device_map": {"1": dev},
            "states": {"1": {"damp_pos": 2}},
        },
        async_add_listener=lambda listener: listeners.append(listener) or (lambda: None),
    )
    runtime = AtmeexRuntimeData(
        api=MagicMock(),
        coordinator=coordinator,
        refresh_device=AsyncMock(),
    )
    entry = SimpleNamespace(
        entry_id="entry1",
        runtime_data=runtime,
        async_on_unload=lambda cb: None,
    )

    entities: list = []

    def _add_entities(new_entities):
        entities.extend(new_entities)

    await async_setup_entry(None, entry, _add_entities)

    assert [type(entity) for entity in entities] == [AtmeexBreezerSelect]
    assert len(listeners) == 1

    coordinator.data["states"]["1"]["hum_stg"] = 1
    listeners[0]()

    assert {type(entity) for entity in entities} == {
        AtmeexBreezerSelect,
        AtmeexHumidificationSelect,
    }
    humidification = next(
        entity
        for entity in entities
        if isinstance(entity, AtmeexHumidificationSelect)
    )
    assert humidification._runtime is runtime


# ---------------------------------------------------------------------------
# current_option truth table (new pwr_on-aware logic)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pwr_on, damp_pos, expected_option",
    [
        (True, 0, BREEZER_OPTIONS[0]),   # forced_ventilation
        (True, 1, BREEZER_OPTIONS[1]),   # recirculation
        (True, 2, BREEZER_OPTIONS[2]),   # mixed_mode
        (False, 0, BREEZER_OPTIONS[3]),  # supply_valve
        (False, 1, None),                # contradictory power/mode state
        (False, 2, None),                # contradictory power/mode state
        (None, 1, None),                 # power state is not known yet
    ],
)
def test_breezer_current_option_truth_table(pwr_on, damp_pos, expected_option):
    _, breezer, cond, _, _ = _make_selects({"pwr_on": pwr_on, "damp_pos": damp_pos})
    assert breezer.current_option == expected_option


# ---------------------------------------------------------------------------
# async_select_option — pending tracking
# ---------------------------------------------------------------------------

def _make_breezer_with_runtime(state_overrides: dict | None = None):
    state = {"online": True, "pwr_on": True, "damp_pos": 0}
    if state_overrides:
        state.update(state_overrides)

    dev = AtmeexDevice.from_raw({"id": 1, "name": "Dev1", "model": "m", "online": True})
    coordinator = SimpleNamespace(
        data={"device_map": {"1": dev}, "states": {"1": state}},
        last_update_success=True,
        async_request_refresh=AsyncMock(),
        async_add_listener=lambda cb: (lambda: None),
    )
    api = MagicMock()
    api.set_breezer_mode = AsyncMock()
    refresh_cb = AsyncMock()
    runtime = AtmeexRuntimeData(api=api, coordinator=coordinator, refresh_device=refresh_cb)
    from custom_components.atmeex_cloud.select import AtmeexBreezerSelect as BreezerCls
    breezer = BreezerCls(
        coordinator=coordinator,
        api=api,
        device=dev,
        refresh_device_cb=refresh_cb,
        runtime=runtime,
    )
    return breezer, api, refresh_cb, runtime


@pytest.mark.asyncio
async def test_breezer_select_supply_valve_sets_pending():
    breezer, api, refresh_cb, runtime = _make_breezer_with_runtime()
    await breezer.async_select_option(BREEZER_OPTIONS[3])  # supply_valve

    api.set_breezer_mode.assert_awaited_once_with(1, 3)
    assert runtime.command_executor.value_with_pending(
        1, "pwr_on", True
    ) is False
    assert runtime.command_executor.value_with_pending(1, "damp_pos", 2) == 0
    assert breezer.current_option == BREEZER_OPTIONS[3]


@pytest.mark.asyncio
async def test_breezer_select_recirculation_sets_pending():
    breezer, api, refresh_cb, runtime = _make_breezer_with_runtime()
    await breezer.async_select_option(BREEZER_OPTIONS[1])  # recirculation

    api.set_breezer_mode.assert_awaited_once_with(1, 1)
    assert runtime.command_executor.value_with_pending(1, "damp_pos", 0) == 1
    assert breezer.current_option == BREEZER_OPTIONS[1]


@pytest.mark.asyncio
async def test_humidification_select_uses_executor_pending_value():
    hum, _breezer, _cond, api, coordinator = _make_selects({"hum_stg": 0})
    refresh = AsyncMock()
    runtime = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=refresh,
    )
    hum._runtime = runtime
    hum._refresh_device_cb = refresh

    await hum.async_select_option("3")

    assert runtime.command_executor.value_with_pending(1, "hum_stg", 0) == 3
    assert hum.current_option == "3"
    refresh.assert_awaited_once_with(1)


class _BadOptionString:
    def __str__(self) -> str:
        raise RuntimeError("must not escape validation")


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_name", ["humidification", "breezer"])
@pytest.mark.parametrize(
    "invalid_option",
    [None, True, 1, object(), _BadOptionString()],
)
async def test_selects_reject_malformed_non_string_options(
    entity_name, invalid_option
):
    hum, breezer, _cond, api, coordinator = _make_selects()
    entity = hum if entity_name == "humidification" else breezer

    with pytest.raises(ServiceValidationError) as raised:
        await entity.async_select_option(invalid_option)

    assert raised.value.translation_key == "invalid_command_value"
    api.set_humid_stage.assert_not_awaited()
    api.set_breezer_mode.assert_not_awaited()
    coordinator.async_request_refresh.assert_not_awaited()


def _attach_select_runtime(hum, breezer, api, coordinator):
    refresh = AsyncMock()
    runtime = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=refresh,
    )
    for entity in (hum, breezer):
        entity._runtime = runtime
        entity._refresh_device_cb = refresh
    return refresh, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_name", "option", "api_method"),
    [
        ("humidification", "3", "set_humid_stage"),
        ("breezer", BREEZER_OPTIONS[1], "set_breezer_mode"),
    ],
)
async def test_select_defers_api_call_until_executor_lock_is_acquired(
    entity_name, option, api_method
):
    hum, breezer, _cond, api, coordinator = _make_selects()
    _refresh, runtime = _attach_select_runtime(
        hum, breezer, api, coordinator
    )
    entity = hum if entity_name == "humidification" else breezer
    lock = runtime.get_device_lock(1)
    await lock.acquire()
    task = asyncio.create_task(entity.async_select_option(option))
    await asyncio.sleep(0)

    try:
        getattr(api, api_method).assert_not_called()
    finally:
        if not task.done():
            task.cancel()
        lock.release()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_name", "option", "api_method", "action", "pending_fields"),
    [
        (
            "humidification",
            "3",
            "set_humid_stage",
            "set humidification stage",
            ("hum_stg",),
        ),
        (
            "breezer",
            BREEZER_OPTIONS[3],
            "set_breezer_mode",
            "set breezer mode",
            ("pwr_on", "damp_pos"),
        ),
    ],
)
async def test_select_api_error_is_translated_and_clears_pending(
    entity_name, option, api_method, action, pending_fields
):
    hum, breezer, _cond, api, coordinator = _make_selects({"pwr_on": True})
    refresh, runtime = _attach_select_runtime(
        hum, breezer, api, coordinator
    )
    entity = hum if entity_name == "humidification" else breezer
    error = ApiError(api_method, "failed", status=503)
    getattr(api, api_method).side_effect = error

    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_select_option(option)

    assert raised.value.translation_key == "command_failed"
    assert raised.value.translation_placeholders == {"action": action}
    assert raised.value.__cause__ is error
    refresh.assert_awaited_once_with(1)
    for field in pending_fields:
        assert runtime.get_pending(1, field) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_name", "option", "api_method", "expected_option", "pending_fields"),
    [
        (
            "humidification",
            "3",
            "set_humid_stage",
            "3",
            ("hum_stg",),
        ),
        (
            "breezer",
            BREEZER_OPTIONS[3],
            "set_breezer_mode",
            BREEZER_OPTIONS[3],
            ("pwr_on", "damp_pos"),
        ),
    ],
)
async def test_select_cancellation_recovers_and_clears_pending(
    entity_name, option, api_method, expected_option, pending_fields
):
    hum, breezer, _cond, api, coordinator = _make_selects(
        {"hum_stg": 0, "pwr_on": True, "damp_pos": 0}
    )
    refresh, runtime = _attach_select_runtime(
        hum, breezer, api, coordinator
    )
    entity = hum if entity_name == "humidification" else breezer
    started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_write(*_args):
        started.set()
        await never_release.wait()

    getattr(api, api_method).side_effect = blocked_write
    task = asyncio.create_task(entity.async_select_option(option))
    await started.wait()

    try:
        assert entity.current_option == expected_option
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    refresh.assert_awaited_once_with(1)
    for field in pending_fields:
        assert runtime.get_pending(1, field) is None
