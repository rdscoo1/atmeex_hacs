"""Tests for the Home Assistant task-creation compatibility surface."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.atmeex_cloud.compat import async_create_background_task


@pytest.mark.asyncio
async def test_async_create_background_task_supports_current_ha_signature():
    names: list[str | None] = []

    class FakeHass:
        def async_create_task(self, coro, name=None):
            names.append(name)
            return asyncio.create_task(coro)

    async def work() -> None:
        return

    task = async_create_background_task(FakeHass(), work(), "atmeex-test")
    await task
    assert names == ["atmeex-test"]


@pytest.mark.asyncio
async def test_async_create_background_task_passes_name_to_kwargs_signature():
    calls: list[dict[str, str]] = []

    class FakeHass:
        def async_create_task(self, coro, **kwargs):
            calls.append(kwargs)
            return asyncio.create_task(coro)

    async def work() -> None:
        return

    task = async_create_background_task(FakeHass(), work(), "atmeex-kwargs")
    await task
    assert calls == [{"name": "atmeex-kwargs"}]


@pytest.mark.asyncio
async def test_async_create_background_task_supports_explicit_old_signature():
    called = False

    class FakeHass:
        def async_create_task(self, coro):
            nonlocal called
            called = True
            return asyncio.create_task(coro)

    async def work() -> None:
        return

    task = async_create_background_task(FakeHass(), work(), "atmeex-old")
    await task
    assert called is True


@pytest.mark.asyncio
async def test_async_create_background_task_prefers_ha_background_ownership():
    calls: list[str] = []

    class FakeHass:
        def async_create_background_task(self, coro, name):
            calls.append(name)
            return asyncio.create_task(coro)

        def async_create_task(self, coro, **kwargs):
            raise AssertionError("foreground task helper must not be selected")

    async def work() -> None:
        return

    task = async_create_background_task(
        FakeHass(), work(), "atmeex-background"
    )
    await task
    assert calls == ["atmeex-background"]


def test_async_create_background_task_does_not_retry_scheduler_type_error():
    calls = 0

    class FakeHass:
        def async_create_task(self, coro, **kwargs):
            nonlocal calls
            calls += 1
            raise TypeError("scheduler body failed")

    async def work() -> None:
        return

    coro = work()
    with pytest.raises(TypeError, match="scheduler body failed"):
        async_create_background_task(FakeHass(), coro, "atmeex-failure")
    assert calls == 1
    assert coro.cr_frame is None


@pytest.mark.asyncio
async def test_async_create_background_task_does_not_retry_lookalike_type_error():
    calls = 0
    captured_tasks: list[asyncio.Task[None]] = []

    class FakeHass:
        def async_create_task(self, coro, **kwargs):
            nonlocal calls
            calls += 1
            captured_tasks.append(asyncio.create_task(coro))
            raise TypeError("got an unexpected keyword argument 'name'")

    async def work() -> None:
        return

    coro = work()
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        async_create_background_task(FakeHass(), coro, "atmeex-lookalike")

    results = await asyncio.gather(*captured_tasks, return_exceptions=True)
    assert calls == 1
    assert len(captured_tasks) == 1
    assert len(results) == 1
    assert isinstance(results[0], RuntimeError)
    assert coro.cr_frame is None


def test_async_create_background_task_closes_foreground_creation_failure():
    class FakeHass:
        def async_create_task(self, coro, **kwargs):
            raise RuntimeError("creation failed")

    async def work() -> None:
        return

    coro = work()
    with pytest.raises(RuntimeError, match="creation failed"):
        async_create_background_task(FakeHass(), coro, "atmeex-failure")
    assert coro.cr_frame is None


def test_async_create_background_task_closes_background_creation_failure():
    class FakeHass:
        def async_create_background_task(self, coro, name):
            raise RuntimeError("background creation failed")

    async def work() -> None:
        return

    coro = work()
    with pytest.raises(RuntimeError, match="background creation failed"):
        async_create_background_task(FakeHass(), coro, "atmeex-background")
    assert coro.cr_frame is None
