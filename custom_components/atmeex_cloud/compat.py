"""Small Home Assistant compatibility surface."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import inspect
from typing import Any, TypeVar

from homeassistant.core import HomeAssistant

_T = TypeVar("_T")


def _supports_name_keyword(creator: Callable[..., Any]) -> bool | None:
    """Return whether a callable explicitly accepts name as a keyword."""
    try:
        parameters = inspect.signature(creator).parameters.values()
    except (TypeError, ValueError):
        return None

    for parameter in parameters:
        if parameter.name == "name":
            return parameter.kind is not inspect.Parameter.POSITIONAL_ONLY
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    ):
        return None
    return False


def _create_task(
    creator: Callable[..., asyncio.Task[_T]],
    coro: Coroutine[Any, Any, _T],
    *args: Any,
    **kwargs: Any,
) -> asyncio.Task[_T]:
    """Transfer coroutine ownership only after task creation succeeds."""
    try:
        return creator(coro, *args, **kwargs)
    except BaseException:
        coro.close()
        raise


def async_create_background_task(
    hass: HomeAssistant,
    coro: Coroutine[Any, Any, _T],
    name: str,
) -> asyncio.Task[_T]:
    """Create a task on old and current supported Home Assistant releases."""
    background_creator = getattr(hass, "async_create_background_task", None)
    if callable(background_creator):
        return _create_task(background_creator, coro, name)

    task_creator = hass.async_create_task
    supports_name = _supports_name_keyword(task_creator)
    if supports_name is False:
        return _create_task(task_creator, coro)
    return _create_task(task_creator, coro, name=name)
