import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.atmeex_cloud as atmeex_init
from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.const import DOMAIN, PLATFORMS
from custom_components.atmeex_cloud.runtime import AtmeexRuntimeData


def _loaded_runtime(*, unload_result=True, unload_error=None):
    manager = SimpleNamespace(disconnect=AsyncMock())

    async def refresh_device(_device_id):
        return None

    runtime = AtmeexRuntimeData(
        api=MagicMock(),
        coordinator=MagicMock(),
        state_store=MagicMock(),
        command_executor=MagicMock(),
        refresh_device=refresh_device,
        websocket_manager=manager,
    )
    entry = SimpleNamespace(entry_id="entry1", runtime_data=runtime)
    unload_platforms = AsyncMock(return_value=unload_result)
    if unload_error is not None:
        unload_platforms.side_effect = unload_error
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_unload_platforms=unload_platforms,
        ),
    )
    return runtime, entry, hass


def _late_startup_runtime(monkeypatch):
    import custom_components.atmeex_cloud.websocket as websocket_mod

    device = AtmeexDevice.from_raw(
        {
            "id": 1,
            "name": "Device",
            "model": "m",
            "online": True,
            "condition": {"pwr_on": 1},
            "settings": {},
        }
    )

    class FakeApi:
        def __init__(self, session, *, on_refresh_token_changed=None):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self.refresh_token = None
            self.token = "token"
            self.get_devices = AsyncMock(return_value=[device])
            self.get_device = AsyncMock(return_value=device)
            self.async_refresh_access_token = AsyncMock()

    class FakeCoordinator:
        def __init__(
            self,
            hass,
            logger,
            *,
            api,
            state_store,
            config_entry_id,
            config_entry=None,
            name,
            update_interval,
            fire_logbook_event=None,
        ):
            self.state_store = state_store
            self.data = None
            self.update_calls = 0
            self.async_request_refresh = AsyncMock()
            self.async_update_listeners = MagicMock()

        async def async_config_entry_first_refresh(self):
            update = self.state_store.apply_inventory(
                [device],
                self.state_store.capture_all(),
            )
            self.data = update.data

        def async_set_updated_data(self, data):
            self.update_calls += 1
            self.data = data

    controls = SimpleNamespace(
        connect_started=asyncio.Event(),
        cancellation_seen=asyncio.Event(),
        release_connect=asyncio.Event(),
        child_release=asyncio.Event(),
    )
    manager = SimpleNamespace(
        task_factory=None,
        on_message=None,
        transport_live=False,
        accepted_after_stop=None,
        child_coroutine=None,
        task_rejection=None,
        listener_tasks=[],
    )

    async def late_listener() -> None:
        await controls.child_release.wait()

    async def connect() -> bool:
        controls.connect_started.set()
        while not controls.release_connect.is_set():
            try:
                await controls.release_connect.wait()
            except asyncio.CancelledError:
                controls.cancellation_seen.set()
        manager.transport_live = True
        manager.accepted_after_stop = manager.on_message(
            {
                "type": "condition",
                "data": [{"id": 1, "condition": {"pwr_on": 0}}],
            }
        )
        child_coro = late_listener()
        manager.child_coroutine = child_coro
        try:
            child = manager.task_factory(
                child_coro,
                "atmeex websocket late listener",
            )
        except RuntimeError as err:
            manager.task_rejection = err
        else:
            manager.listener_tasks.append(child)
        return True

    async def disconnect() -> None:
        manager.transport_live = False
        for task in tuple(manager.listener_tasks):
            task.cancel()
        await asyncio.gather(
            *tuple(manager.listener_tasks),
            return_exceptions=True,
        )
        manager.listener_tasks.clear()

    manager.connect = AsyncMock(side_effect=connect)
    manager.disconnect = AsyncMock(side_effect=disconnect)

    def create_manager(**kwargs):
        manager.task_factory = kwargs["task_factory"]
        manager.on_message = kwargs["on_message"]
        return manager

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", FakeCoordinator)
    monkeypatch.setattr(websocket_mod, "WebSocketManager", create_manager)
    monkeypatch.setattr(
        atmeex_init,
        "async_get_clientsession",
        lambda hass: SimpleNamespace(ws_connect=AsyncMock()),
    )
    hass = SimpleNamespace(
        bus=SimpleNamespace(async_fire=MagicMock()),
        async_create_task=lambda coro, **kwargs: asyncio.create_task(
            coro,
            name=kwargs.get("name"),
        ),
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "secret"},
        options={"enable_websocket": True},
        entry_id="entry1",
        runtime_data=None,
        add_update_listener=lambda callback: lambda: None,
        async_on_unload=lambda callback: None,
        async_start_reauth=MagicMock(),
    )
    return entry, hass, manager, controls


@pytest.mark.asyncio
async def test_platform_unload_false_leaves_runtime_operational():
    runtime, entry, hass = _loaded_runtime(unload_result=False)
    task = runtime.track_task(asyncio.create_task(asyncio.Event().wait()))
    runtime.websocket_start_task = task
    runtime.refresh_tasks["1"] = task
    await asyncio.sleep(0)

    try:
        assert await atmeex_init.async_unload_entry(hass, entry) is False

        assert runtime.stopping is False
        assert task.cancelled() is False
        assert runtime.tasks == {task}
        assert runtime.websocket_start_task is task
        assert runtime.refresh_tasks == {"1": task}
        runtime.websocket_manager.disconnect.assert_not_awaited()
        assert entry.runtime_data is runtime
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_platform_unload_error_leaves_runtime_operational():
    runtime, entry, hass = _loaded_runtime(
        unload_error=RuntimeError("platform unload failed"),
    )
    task = runtime.track_task(asyncio.create_task(asyncio.Event().wait()))
    runtime.websocket_start_task = task
    await asyncio.sleep(0)

    try:
        with pytest.raises(RuntimeError, match="platform unload failed"):
            await atmeex_init.async_unload_entry(hass, entry)

        assert runtime.stopping is False
        assert task.cancelled() is False
        assert runtime.tasks == {task}
        assert runtime.websocket_start_task is task
        runtime.websocket_manager.disconnect.assert_not_awaited()
        assert entry.runtime_data is runtime
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_websocket_startup_cannot_resurrect_after_unload(
    monkeypatch,
):
    entry, hass, manager, controls = _late_startup_runtime(monkeypatch)
    monkeypatch.setattr(atmeex_init, "_UNLOAD_TASK_TIMEOUT_SEC", 0.0)

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    runtime = entry.runtime_data
    startup = runtime.websocket_start_task
    assert startup is not None
    await asyncio.wait_for(controls.connect_started.wait(), timeout=1.0)

    try:
        assert await asyncio.wait_for(
            atmeex_init.async_unload_entry(hass, entry),
            timeout=1.0,
        ) is True
        await asyncio.sleep(0)

        assert controls.cancellation_seen.is_set()
        assert entry.runtime_data is None
        assert runtime.stopping is True
        assert runtime.websocket_start_task is None
        assert startup in runtime.tasks
        assert startup.done() is False
        manager.disconnect.assert_not_awaited()

        controls.release_connect.set()
        await asyncio.wait_for(asyncio.shield(startup), timeout=1.0)
        await asyncio.sleep(0)

        manager.disconnect.assert_awaited_once_with()
        assert manager.transport_live is False
        assert manager.accepted_after_stop is False
        assert manager.listener_tasks == []
        assert isinstance(manager.task_rejection, RuntimeError)
        assert manager.child_coroutine is not None
        assert manager.child_coroutine.cr_frame is None
        assert runtime.tasks == set()
        assert runtime.coordinator.update_calls == 0
        assert not any(
            call.args and call.args[0] == atmeex_init.EVENT_DEVICE_UPDATED
            for call in hass.bus.async_fire.call_args_list
        )
    finally:
        controls.release_connect.set()
        controls.child_release.set()
        remaining = tuple(runtime.tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


@pytest.mark.asyncio
async def test_successful_unload_closes_socket_and_awaits_all_tasks():
    runtime, entry, hass = _loaded_runtime(unload_result=True)
    order = []
    spawned_children = []
    publication = MagicMock()
    never = asyncio.Event()

    async def cancellation_child():
        try:
            await never.wait()
        finally:
            order.append("child stopped")

    async def startup_producer():
        try:
            await never.wait()
        finally:
            order.append("startup stopped")
            child = runtime.track_task(
                asyncio.create_task(cancellation_child())
            )
            spawned_children.append(child)
            await asyncio.sleep(0)

    async def callback_producer():
        try:
            await never.wait()
        finally:
            if not runtime.stopping:
                publication()

    startup = runtime.track_task(asyncio.create_task(startup_producer()))
    callback = runtime.track_task(asyncio.create_task(callback_producer()))
    runtime.websocket_start_task = startup
    runtime.websocket_message_task = callback
    runtime.websocket_resync_task = callback
    runtime.inventory_watchdog_task = callback
    runtime.refresh_tasks["1"] = callback

    async def disconnect():
        assert startup.done()
        order.append("disconnect")

    runtime.websocket_manager.disconnect.side_effect = disconnect
    await asyncio.sleep(0)

    try:
        assert await atmeex_init.async_unload_entry(hass, entry) is True

        assert order == ["startup stopped", "disconnect", "child stopped"]
        assert len(spawned_children) == 1
        assert spawned_children[0].done()
        assert callback.done()
        publication.assert_not_called()
        runtime.websocket_manager.disconnect.assert_awaited_once()
        assert runtime.tasks == set()
        assert runtime.refresh_tasks == {}
        assert runtime.websocket_start_task is None
        assert runtime.websocket_message_task is None
        assert runtime.websocket_resync_task is None
        assert runtime.inventory_watchdog_task is None
        assert entry.runtime_data is None
    finally:
        never.set()
        remaining = tuple(runtime.tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


@pytest.mark.asyncio
async def test_unload_hung_task_is_bounded_and_retained_for_diagnostics(
    monkeypatch,
    caplog,
):
    runtime, entry, hass = _loaded_runtime(unload_result=True)
    gate = asyncio.Event()

    async def hung_task():
        try:
            await asyncio.Event().wait()
        finally:
            await gate.wait()

    task = runtime.track_task(asyncio.create_task(hung_task()))
    runtime.websocket_start_task = task
    runtime.refresh_tasks["1"] = task
    await asyncio.sleep(0)
    monkeypatch.setattr(atmeex_init, "_UNLOAD_TASK_TIMEOUT_SEC", 0.01)

    try:
        with caplog.at_level(logging.WARNING):
            result = await asyncio.wait_for(
                atmeex_init.async_unload_entry(hass, entry),
                timeout=0.1,
            )

        assert result is True
        assert task in runtime.tasks
        assert not task.done()
        assert runtime.refresh_tasks == {}
        assert runtime.websocket_start_task is None
        assert entry.runtime_data is None
        assert "1 Atmeex tasks exceeded the unload timeout" in caplog.text
    finally:
        gate.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unload_disconnect_error_is_typed_and_does_not_leak(caplog):
    runtime, entry, hass = _loaded_runtime(unload_result=True)
    runtime.websocket_manager.disconnect.side_effect = RuntimeError(
        "private socket detail"
    )

    with caplog.at_level(logging.WARNING):
        result = await atmeex_init.async_unload_entry(hass, entry)

    assert result is True
    assert entry.runtime_data is None
    assert "Atmeex WebSocket cleanup failed: RuntimeError" in caplog.text
    assert "private socket detail" not in caplog.text


@pytest.mark.asyncio
async def test_successful_platform_unload_clears_runtime_if_cleanup_raises(
    monkeypatch,
):
    _runtime, entry, hass = _loaded_runtime(unload_result=True)
    cleanup = AsyncMock(side_effect=RuntimeError("cleanup failed"))
    monkeypatch.setattr(atmeex_init, "_async_cleanup_runtime", cleanup)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await atmeex_init.async_unload_entry(hass, entry)

    cleanup.assert_awaited_once()
    assert entry.runtime_data is None


async def test_async_remove_config_entry_device_drops_per_device_state():
    """Device removal must evict per-device locks and pending commands.

    Without this cleanup, runtime.device_locks / runtime.pending_commands grow
    unboundedly across add/remove cycles for the lifetime of the loaded entry.
    """
    runtime = AtmeexRuntimeData(
        api=None,
        coordinator=None,
        refresh_device=AsyncMock(),
    )
    runtime.get_device_lock("42")
    runtime.get_device_lock("99")
    runtime.set_pending("42", "pwr_on", True)
    runtime.set_pending("99", "pwr_on", False)

    entry = SimpleNamespace(runtime_data=runtime)
    device_entry = SimpleNamespace(identifiers={(DOMAIN, "42"), ("other_domain", "ignored")})

    result = await atmeex_init.async_remove_config_entry_device(
        hass=SimpleNamespace(), config_entry=entry, device_entry=device_entry
    )

    assert result is True
    assert "42" not in runtime.device_locks
    assert "42" not in runtime.pending_commands
    # Other devices and unrelated identifiers are untouched.
    assert "99" in runtime.device_locks
    assert "99" in runtime.pending_commands

async def test_async_remove_config_entry_device_handles_missing_runtime():
    """Removal must not crash when runtime_data is unset (e.g. failed setup).

    Without runtime we cannot confirm the device is absent from the
    authoritative inventory, so removal is refused rather than allowed.
    """
    entry = SimpleNamespace()  # no runtime_data attribute
    device_entry = SimpleNamespace(identifiers={(DOMAIN, "42")})

    result = await atmeex_init.async_remove_config_entry_device(
        hass=SimpleNamespace(), config_entry=entry, device_entry=device_entry
    )
    assert result is False

async def test_async_remove_config_entry_device_unknown_device_is_noop():
    """Removing a device that was never tracked must not raise."""
    runtime = AtmeexRuntimeData(
        api=None,
        coordinator=None,
        refresh_device=AsyncMock(),
    )
    entry = SimpleNamespace(runtime_data=runtime)
    device_entry = SimpleNamespace(identifiers={(DOMAIN, "never_seen")})

    result = await atmeex_init.async_remove_config_entry_device(
        hass=SimpleNamespace(), config_entry=entry, device_entry=device_entry
    )
    assert result is True
    assert runtime.device_locks == {}
    assert runtime.pending_commands == {}


async def test_async_remove_config_entry_device_uses_canonical_executor_cleanup():
    """Removal must preserve a held compatibility lock until it is released."""
    runtime = AtmeexRuntimeData(
        api=None,
        coordinator=None,
        refresh_device=AsyncMock(),
    )
    lock = runtime.get_device_lock("0042")
    await lock.acquire()
    runtime.set_pending(42, "pwr_on", True)
    entry = SimpleNamespace(runtime_data=runtime)
    device_entry = SimpleNamespace(identifiers={(DOMAIN, "42")})

    assert await atmeex_init.async_remove_config_entry_device(
        hass=SimpleNamespace(),
        config_entry=entry,
        device_entry=device_entry,
    ) is True

    assert runtime.get_pending(42, "pwr_on") is None
    assert runtime.get_device_lock(42) is lock
    lock.release()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert runtime.device_locks == {}
