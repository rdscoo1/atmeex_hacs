import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from custom_components.atmeex_cloud import async_setup_entry
from custom_components.atmeex_cloud.const import DOMAIN
import custom_components.atmeex_cloud as atmeex_init
from custom_components.atmeex_cloud.api import (
    AtmeexConnectionError,
    AtmeexDevice,
    AtmeexProtocolError,
)
from tests.conftest import DummyCoordinator


class FakeApi:
    """Фейковый API для проверки refresh_device без реального HA."""

    def __init__(self, session, *, on_refresh_token_changed=None):
        self.session = session
        self.on_refresh_token_changed = on_refresh_token_changed
        # начальное состояние: устройство включено
        dev_initial_raw = {
            "id": 1,
            "name": "Dev1",
            "model": "test-model",
            "online": True,
            "condition": {"pwr_on": 1, "fan_speed": 3},
            "settings": {},
        }
        self._dev_initial = AtmeexDevice.from_raw(dev_initial_raw)

        # состояние после refresh_device: устройство выключено
        dev_refreshed_raw = {
            "id": 1,
            "name": "Dev1",
            "model": "test-model",
            "online": True,
            "condition": {"pwr_on": 0, "fan_speed": 3},
            "settings": {},
        }
        self._dev_refreshed = AtmeexDevice.from_raw(dev_refreshed_raw)

        self.async_init = AsyncMock()
        self.refresh_token = None
        self.login = AsyncMock()

        # первый полный опрос — список устройств (включённое)
        self.get_devices = AsyncMock(return_value=[self._dev_initial])

        # считаем вызовы get_device
        self._get_device_call_count = 0

        def _get_device_side_effect(device_id):
            """Первая дочитка — включённый девайс, далее — выключенный."""
            self._get_device_call_count += 1
            if self._get_device_call_count == 1:
                return self._dev_initial
            return self._dev_refreshed

        # точечное дочтение: сначала включённый, затем выключенный
        self.get_device = AsyncMock(side_effect=_get_device_side_effect)


@pytest.mark.asyncio
async def test_refresh_device_updates_coordinator_data(monkeypatch):
    # подменяем AtmeexApi на наш фейк
    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

    # подменяем async_get_clientsession, чтобы не создавать реальную сессию
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda hass: object())

    # hass-заглушка без реального Home Assistant
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )

    # entry-заглушка с нужными полями
    entry = SimpleNamespace(
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
        options={},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )


    # запуск setup_entry создаст FakeApi, DummyCoordinator и refresh_device
    result = await async_setup_entry(hass, entry)
    assert result is True

    runtime = entry.runtime_data
    coordinator = runtime.coordinator

    # sanity-check: после первого refresh устройство есть и pwr_on=True
    state_before = coordinator.data["states"]["1"]
    assert state_before["pwr_on"] is True

    # вызываем refresh_device
    await runtime.refresh_device(1)

    state_after = coordinator.data["states"]["1"]
    assert state_after["pwr_on"] is False


@pytest.mark.asyncio
async def test_refresh_device_preserves_newer_websocket_field(monkeypatch):
    get_started = asyncio.Event()
    release_get = asyncio.Event()

    initial = AtmeexDevice.from_raw(
        {
            "id": 1,
            "name": "Dev1",
            "model": "test-model",
            "online": True,
            "condition": {"pwr_on": 1, "fan_speed": 3, "temp_in": 200},
            "settings": {},
        }
    )
    stale_refresh = AtmeexDevice.from_raw(
        {
            "id": 1,
            "name": "Dev1",
            "model": "test-model",
            "online": True,
            "condition": {"pwr_on": 1, "fan_speed": 2, "temp_in": 225},
            "settings": {},
        }
    )

    class BlockingApi:
        def __init__(self, session, *, on_refresh_token_changed=None):
            self.session = session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            self.get_devices = AsyncMock(return_value=[initial])
            self._get_count = 0

        async def get_device(self, _device_id):
            self._get_count += 1
            if self._get_count == 1:
                return initial
            get_started.set()
            await release_get.wait()
            return stale_refresh

    monkeypatch.setattr(atmeex_init, "AtmeexApi", BlockingApi)
    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda hass: object())

    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
        options={"enable_websocket": False},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    assert await async_setup_entry(hass, entry) is True
    runtime = entry.runtime_data
    refresh_task = asyncio.create_task(runtime.refresh_device(1))
    await get_started.wait()

    try:
        runtime.state_store.apply_websocket_delta(
            "1",
            state_delta={"fan_speed": 7},
            device_delta={"condition": {"fan_speed": 6}},
        )
    finally:
        release_get.set()
        await refresh_task

    assert runtime.coordinator.data["states"]["1"]["fan_speed"] == 7
    assert runtime.coordinator.data["states"]["1"]["temp_in"] == 225


async def _setup_default_refresh_runtime(monkeypatch, hass):
    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: object())
    entry = SimpleNamespace(
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
        options={"enable_websocket": False},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )
    assert await async_setup_entry(hass, entry) is True
    return entry.runtime_data


@pytest.mark.asyncio
async def test_refresh_device_typed_error_propagates_and_schedules_recovery(
    monkeypatch,
):
    created_tasks: list[asyncio.Task] = []

    def _create_task(coro, *, name=None):
        task = asyncio.create_task(coro, name=name)
        created_tasks.append(task)
        return task

    hass = SimpleNamespace(
        data={},
        bus=SimpleNamespace(async_fire=MagicMock()),
        async_create_task=_create_task,
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    runtime = await _setup_default_refresh_runtime(monkeypatch, hass)
    runtime.coordinator.async_request_refresh = AsyncMock()
    failure = AtmeexConnectionError(
        "get_device",
        "cloud unavailable",
        status=503,
    )
    runtime.api.get_device.side_effect = failure

    with pytest.raises(AtmeexConnectionError) as caught:
        await runtime.refresh_device(1)

    assert caught.value is failure
    await asyncio.gather(*created_tasks)
    runtime.coordinator.async_request_refresh.assert_awaited_once_with()
    assert any(
        call.args
        and call.args[0] == atmeex_init.EVENT_API_ERROR
        and call.args[1]["source"] == "refresh_device"
        for call in hass.bus.async_fire.call_args_list
    )


@pytest.mark.asyncio
async def test_refresh_device_closes_recovery_when_task_creation_fails(
    monkeypatch,
):
    captured_coroutines = []

    def _reject_task(coro, *, name=None):
        captured_coroutines.append(coro)
        raise RuntimeError("scheduler unavailable")

    hass = SimpleNamespace(
        data={},
        async_create_task=_reject_task,
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    runtime = await _setup_default_refresh_runtime(monkeypatch, hass)
    runtime.coordinator.async_request_refresh = AsyncMock()
    runtime.api.get_device.side_effect = AtmeexConnectionError(
        "get_device",
        "cloud unavailable",
    )

    with pytest.raises(RuntimeError, match="scheduler unavailable"):
        await runtime.refresh_device(1)

    assert len(captured_coroutines) == 1
    assert captured_coroutines[0].cr_frame is None


@pytest.mark.asyncio
async def test_refresh_device_malformed_snapshot_preserves_exact_store(
    monkeypatch,
):
    created_tasks: list[asyncio.Task] = []

    def _create_task(coro, *, name=None):
        task = asyncio.create_task(coro, name=name)
        created_tasks.append(task)
        return task

    hass = SimpleNamespace(
        data={},
        async_create_task=_create_task,
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    runtime = await _setup_default_refresh_runtime(monkeypatch, hass)
    runtime.coordinator.async_request_refresh = AsyncMock()
    malformed = AtmeexDevice.from_raw(
        {
            "id": 1,
            "name": "Dev1",
            "model": "test-model",
            "online": True,
            "condition": {"pwr_on": 1, "fan_speed": 3},
            "settings": {},
        }
    )
    malformed.raw["condition"]["pwr_on"] = "definitely-not-a-boolean"
    runtime.api.get_device.side_effect = None
    runtime.api.get_device.return_value = malformed
    prior = runtime.state_store.data

    with pytest.raises(AtmeexProtocolError):
        await runtime.refresh_device(1)

    await asyncio.gather(*created_tasks)
    assert runtime.state_store.data is prior
    assert runtime.coordinator.data is prior
