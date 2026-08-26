import asyncio
import logging
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


def _ha_create_task(coro, *, name=None):
    """Create a named task like HomeAssistant.async_create_task."""
    return asyncio.create_task(coro, name=name)


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

        def _get_device_side_effect(device_id):
            """Targeted refresh returns the newer device snapshot."""
            return self._dev_refreshed

        # The inventory list provides initial state; targeted reads provide updates.
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
        async_create_task=_ha_create_task,
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

    runtime.api.get_device.reset_mock()

    async def operation() -> None:
        return

    await runtime.command_executor.async_execute(
        1,
        operation,
        pending={"pwr_on": False},
        translation_key="command_failed",
    )

    runtime.api.get_device.assert_awaited_once_with(1)
    assert runtime.coordinator.data["states"]["1"]["pwr_on"] is False


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

        async def get_device(self, _device_id):
            get_started.set()
            await release_get.wait()
            return stale_refresh

    monkeypatch.setattr(atmeex_init, "AtmeexApi", BlockingApi)
    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda hass: object())

    hass = SimpleNamespace(
        data={},
        async_create_task=_ha_create_task,
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
    runtime.coordinator.async_update_listeners = MagicMock()
    failure = AtmeexConnectionError(
        "get_device",
        "cloud unavailable",
        status=503,
    )
    runtime.api.get_device.side_effect = failure

    with pytest.raises(AtmeexConnectionError) as caught:
        await runtime.refresh_device(1)

    assert caught.value is failure
    recovery_tasks = [
        task
        for task in created_tasks
        if task.get_name() == "atmeex targeted-refresh recovery"
    ]
    assert len(recovery_tasks) == 1
    await asyncio.gather(*recovery_tasks)
    runtime.coordinator.async_request_refresh.assert_awaited_once_with()
    assert any(
        call.args
        and call.args[0] == atmeex_init.EVENT_API_ERROR
        and call.args[1]["source"] == "refresh_device"
        for call in hass.bus.async_fire.call_args_list
    )


@pytest.mark.asyncio
async def test_failed_authoritative_recovery_keeps_pending_unconfirmed(
    monkeypatch,
):
    """An absorbed coordinator failure must not authorize confirmation."""
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
    coordinator = runtime.coordinator
    executor = runtime.command_executor
    allow_recovery = MagicMock(
        wraps=executor.allow_recovery_confirmation,
    )
    executor.allow_recovery_confirmation = allow_recovery
    coordinator.async_update_listeners = MagicMock()

    async def absorbed_failed_refresh() -> None:
        coordinator.last_update_success = False

    coordinator.async_request_refresh = AsyncMock(
        side_effect=absorbed_failed_refresh,
    )
    runtime.api.get_device.side_effect = AtmeexConnectionError(
        "get_device",
        "cloud unavailable",
        status=503,
    )

    confirmation = await executor.async_execute(
        1,
        AsyncMock(),
        pending={"pwr_on": False},
        translation_key="command_failed",
    )
    assert confirmation is False
    pending_before = executor.get_pending(1, "pwr_on")
    assert pending_before is not None

    recovery_tasks = [
        task
        for task in created_tasks
        if task.get_name() == "atmeex targeted-refresh recovery"
    ]
    assert len(recovery_tasks) == 1
    await asyncio.gather(*recovery_tasks)

    assert coordinator.last_update_success is False
    coordinator.async_request_refresh.assert_awaited_once_with()
    allow_recovery.assert_not_called()
    coordinator.async_update_listeners.assert_not_called()
    pending_after = executor.get_pending(1, "pwr_on")
    assert pending_after is not None
    assert pending_after.generation == pending_before.generation
    assert pending_after.value is False


@pytest.mark.asyncio
async def test_debounced_recovery_keeps_pending_without_new_inventory(
    monkeypatch,
):
    """A stale prior success must not authorize recovery confirmation."""
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
    coordinator = runtime.coordinator
    executor = runtime.command_executor
    prior_inventory_success = coordinator.last_inventory_success_mono
    assert coordinator.last_update_success is True
    assert prior_inventory_success is not None
    allow_recovery = MagicMock(
        wraps=executor.allow_recovery_confirmation,
    )
    executor.allow_recovery_confirmation = allow_recovery
    coordinator.async_update_listeners = MagicMock()
    coordinator.async_request_refresh = AsyncMock()
    runtime.api.get_device.side_effect = AtmeexConnectionError(
        "get_device",
        "cloud unavailable",
        status=503,
    )

    confirmation = await executor.async_execute(
        1,
        AsyncMock(),
        pending={"pwr_on": False},
        translation_key="command_failed",
    )
    assert confirmation is False
    pending_before = executor.get_pending(1, "pwr_on")
    assert pending_before is not None

    recovery_tasks = [
        task
        for task in created_tasks
        if task.get_name() == "atmeex targeted-refresh recovery"
    ]
    assert len(recovery_tasks) == 1
    await asyncio.gather(*recovery_tasks)

    assert coordinator.last_update_success is True
    assert coordinator.last_inventory_success_mono == prior_inventory_success
    coordinator.async_request_refresh.assert_awaited_once_with()
    allow_recovery.assert_not_called()
    coordinator.async_update_listeners.assert_not_called()
    pending_after = executor.get_pending(1, "pwr_on")
    assert pending_after is not None
    assert pending_after.generation == pending_before.generation
    assert pending_after.value is False


@pytest.mark.asyncio
async def test_degraded_inventory_recovery_keeps_pending_unconfirmed(
    monkeypatch,
):
    """A newer but degraded inventory must not authorize confirmation."""
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
    coordinator = runtime.coordinator
    executor = runtime.command_executor
    prior_inventory_success = coordinator.last_inventory_success_mono
    allow_recovery = MagicMock(
        wraps=executor.allow_recovery_confirmation,
    )
    executor.allow_recovery_confirmation = allow_recovery
    coordinator.async_update_listeners = MagicMock()
    runtime.api.get_devices.return_value = [
        AtmeexDevice.from_raw(
            {
                "id": 1,
                "name": "Dev1",
                "model": "test-model",
                "online": True,
            }
        )
    ]
    runtime.api.get_device.side_effect = AtmeexConnectionError(
        "get_device",
        "cloud unavailable",
        status=503,
    )

    confirmation = await executor.async_execute(
        1,
        AsyncMock(),
        pending={"pwr_on": False},
        translation_key="command_failed",
    )
    assert confirmation is False

    recovery_tasks = [
        task
        for task in created_tasks
        if task.get_name() == "atmeex targeted-refresh recovery"
    ]
    assert len(recovery_tasks) == 1
    await asyncio.gather(*recovery_tasks)

    assert coordinator.last_update_success is True
    assert coordinator.last_inventory_success_mono > prior_inventory_success
    assert isinstance(coordinator.last_api_error, AtmeexConnectionError)
    allow_recovery.assert_not_called()
    coordinator.async_update_listeners.assert_not_called()
    assert executor.get_pending(1, "pwr_on") is not None


@pytest.mark.asyncio
async def test_unexpected_refresh_failure_is_private_pending_and_recovered(
    monkeypatch,
    caplog,
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
    runtime.coordinator.async_update_listeners = MagicMock()
    secret = "private-token-should-never-leak"
    runtime.api.get_device.side_effect = RuntimeError(secret)
    caplog.set_level(logging.WARNING)

    confirmation = await runtime.command_executor.async_execute(
        1,
        AsyncMock(),
        pending={"pwr_on": False},
        translation_key="command_failed",
    )

    assert confirmation is False
    pending = runtime.command_executor.get_pending(1, "pwr_on")
    assert pending is not None
    assert pending.value is False

    owner_tasks = [
        task
        for task in created_tasks
        if task.get_name() == "atmeex refresh 1"
    ]
    assert len(owner_tasks) == 1
    sanitized_failure = owner_tasks[0].exception()
    assert isinstance(sanitized_failure, AtmeexProtocolError)
    assert sanitized_failure.__context__ is None
    assert str(sanitized_failure) == "get_device: unexpected client failure"

    recovery_tasks = [
        task
        for task in created_tasks
        if task.get_name() == "atmeex targeted-refresh recovery"
    ]
    assert len(recovery_tasks) == 1
    await asyncio.gather(*recovery_tasks)
    runtime.coordinator.async_request_refresh.assert_awaited_once_with()

    api_error_calls = [
        call
        for call in hass.bus.async_fire.call_args_list
        if call.args
        and call.args[0] == atmeex_init.EVENT_API_ERROR
        and call.args[1]["source"] == "refresh_device"
    ]
    assert len(api_error_calls) == 1
    payload = api_error_calls[0].args[1]
    assert payload["operation"] == "get_device"
    assert payload["message"] == "get_device: unexpected client failure"
    assert secret not in caplog.text
    assert secret not in repr(hass.bus.async_fire.call_args_list)
    # The warning must correlate the device via its anonymized label, never
    # the raw cloud device ID.
    from custom_components.atmeex_cloud.privacy import anonymous_device_label

    assert f"Failed to refresh {anonymous_device_label(1)}" in caplog.text
    assert "Failed to refresh device 1" not in caplog.text


@pytest.mark.asyncio
async def test_refresh_device_closes_recovery_when_task_creation_fails(
    monkeypatch,
):
    captured_coroutines = []

    def _reject_recovery_task(coro, *, name=None):
        if name == "atmeex targeted-refresh recovery":
            captured_coroutines.append(coro)
            raise RuntimeError("scheduler unavailable")
        return asyncio.create_task(coro, name=name)

    hass = SimpleNamespace(
        data={},
        async_create_task=_reject_recovery_task,
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

    await asyncio.gather(*created_tasks, return_exceptions=True)
    assert runtime.state_store.data is prior
    assert runtime.coordinator.data is prior
