from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable, Iterable

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo

from .api import AtmeexDevice
from .command_executor import (
    AtmeexCommandExecutor,
    CommandCoroutineFactory,
    RecoveryRefresh,
)
from .const import DOMAIN
from .helpers import parse_atmeex_bool


def _api_fan_target(value: Any) -> int:
    """Strictly convert a stored API fan speed (0..6) to the HA scale (1..7).

    Malformed or out-of-range targets raise, so they count as missing and the
    requested speed is sent.
    """
    speed = int(value)
    if not 0 <= speed <= 6:
        raise ValueError(value)
    return speed + 1


# State field -> (cloud settings key, converter into the state field's scale).
_SETTING_FOR_FIELD: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "pwr_on": ("u_pwr_on", parse_atmeex_bool),
    "fan_speed": ("u_fan_speed", _api_fan_target),
    "u_temp_room": ("u_temp_room", int),
    "damp_pos": ("u_damp_pos", int),
    "hum_stg": ("u_hum_stg", int),
    "u_auto": ("u_auto", parse_atmeex_bool),
    "u_night": ("u_night", parse_atmeex_bool),
}


class AtmeexEntityMixin:
    """Общее для всех сущностей Atmeex."""

    coordinator: Any  # CoordinatorEntity уже дает .coordinator
    _device_id: int | str
    _device_meta: AtmeexDevice
    _refresh_device_cb: RecoveryRefresh | None = None

    @property
    def _device_id_str(self) -> str:
        return str(self._device_id)

    @property
    def _device(self) -> AtmeexDevice:
        """Текущий девайс из device_map, fallback на _device_meta."""
        data = getattr(self.coordinator, "data", None) or {}
        device_map = data.get("device_map", {}) or {}
        return device_map.get(self._device_id_str) or self._device_meta
    
    @property
    def _device_state(self) -> dict[str, Any]:
        data = getattr(self.coordinator, "data", None) or {}
        return (data.get("states", {}) or {}).get(self._device_id_str, {}) or {}

    async def _refresh(self) -> None:
        """Refresh this device, preferring targeted refresh callback when available."""
        if callable(self._refresh_device_cb):
            await self._refresh_device_cb(self._device_id)
            return
        await self.coordinator.async_request_refresh()

    def _state_with_pending(
        self,
        attribute: str,
        confirmed_value: Any,
    ) -> Any:
        """Return the executor's optimistic value while it awaits confirmation."""
        runtime = getattr(self, "_runtime", None)
        executor = getattr(runtime, "command_executor", None)
        if executor is None:
            return confirmed_value
        return executor.value_with_pending(
            self._device_id,
            attribute,
            confirmed_value,
        )

    @property
    def _command_executor(self) -> AtmeexCommandExecutor:
        """Entry-owned executor, or a lazily created per-entity fallback."""
        runtime = getattr(self, "_runtime", None)
        executor = getattr(runtime, "command_executor", None)
        if executor is None:
            executor = getattr(self, "_fallback_command_executor", None)
            if executor is None:
                executor = AtmeexCommandExecutor(
                    lambda _device_id: self._refresh()
                )
                self._fallback_command_executor = executor
        return executor

    def _target_setting(self, field: str) -> Any:
        """Return the cloud's stored target for a state field, or None.

        Targets (settings.u_*) are updated by the cloud as soon as it accepts
        a PUT and stay valid while the device is off, unlike the reported
        condition, which lags until the device acknowledges.
        """
        settings_key, convert = _SETTING_FOR_FIELD[field]
        raw = self._device.settings.get(settings_key)
        if raw is None:
            return None
        try:
            return convert(raw)
        except (TypeError, ValueError):
            return None

    def _fields_to_write(self, desired: Mapping[str, Any]) -> dict[str, Any]:
        """Return the desired fields whose cloud target differs.

        Inside a command operation this compares against the target *before*
        that command (stored settings plus older unconfirmed writes), so a
        queued predecessor can never make this command skip a needed write.
        Until a write is confirmed by a successful refresh (after a failed
        write or a failed confirmation) the stored targets may be stale, so
        everything is sent.
        """
        executor = self._command_executor
        if executor.targets_untrusted(self._device_id):
            return dict(desired)
        return {
            field: value
            for field, value in desired.items()
            if executor.settled_value(
                self._device_id, field, self._target_setting(field)
            )
            != value
        }

    def _already_applied(self, desired: Mapping[str, Any]) -> bool:
        """True when nothing is in flight and the device already matches.

        Lets repeated identical calls (e.g. an automation re-sending the same
        fan speed on every CO2 update) cost zero requests.
        """
        return not self._command_executor.is_busy(
            self._device_id
        ) and not self._fields_to_write(desired)

    async def _apply_breezer_mode(self, mode: int) -> None:
        """Set breezer mode 0..3, sending only what changes.

        u_pwr_on rides in the combined body only when it actually changes; with
        power unchanged the damper is sent alone, because the cloud drops
        fields from a combined body whose u_pwr_on is unchanged (fa6b806).
        """
        power = mode != 3
        damper = 0 if mode == 3 else mode
        desired = {"pwr_on": power, "damp_pos": damper}
        if self._already_applied(desired):
            return

        async def operation() -> None:
            writes = self._fields_to_write(desired)
            if "pwr_on" in writes:
                await self.api.set_breezer_mode(self._device_id, mode)
            elif "damp_pos" in writes:
                await self.api.set_damper_position(self._device_id, damper)

        await self._execute_command(
            operation,
            pending=desired,
            translation_placeholders={"action": "set breezer mode"},
        )

    async def _execute_command(
        self,
        operation: CommandCoroutineFactory,
        *,
        pending: Mapping[str, Any],
        translation_key: str = "command_failed",
        translation_placeholders: Mapping[str, str] | None = None,
    ) -> bool:
        """Execute one lazy command through the entry-owned executor."""
        return await self._command_executor.async_execute(
            self._device_id,
            operation,
            pending=pending,
            translation_key=translation_key,
            translation_placeholders=translation_placeholders,
        )

    @staticmethod
    def _invalid_value(field: str, value: Any) -> ServiceValidationError:
        try:
            rendered_value = str(value)
        except Exception:
            rendered_value = f"<{type(value).__name__} outside supported range>"
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_command_value",
            translation_placeholders={"field": field, "value": rendered_value},
        )

    @staticmethod
    def _unsupported_feature(feature: str) -> ServiceValidationError:
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unsupported_device_feature",
            translation_placeholders={"feature": feature},
        )

    @property
    def available(self) -> bool:
        """Entity availability = coordinator health AND device online.

        Honors coordinator health so a stale integration (e.g. the cloud has
        been unreachable for several polls) does not keep presenting last-known
        device state as if it were live.
        """
        coordinator = getattr(self, "coordinator", None)
        if coordinator is not None and not getattr(
            coordinator, "last_update_success", True
        ):
            return False
        st = self._device_state
        if "online" in st:
            return bool(st["online"])
        return bool(getattr(self._device_meta, "online", False))

    @property
    def device_info(self) -> DeviceInfo:
        dev = self._device_meta  # мета фиксирована, не зависит от апдейтов
        # Try to get firmware version from raw device data
        raw = getattr(dev, "raw", {}) or {}
        sw_version = raw.get("firmware_version") or raw.get("fw_version") or raw.get("version")
        
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id_str)},
            name=getattr(dev, "name", None),
            manufacturer="Atmeex",
            model=getattr(dev, "model", None),
            sw_version=sw_version,
        )


def supports_humidifier(state: dict[str, Any] | None) -> bool:
    """Return True when the current device state exposes humidifier features."""
    device_state = state or {}
    return "hum_stg" in device_state or "no_water" in device_state


def setup_dynamic_device_entities(
    *,
    entry: Any,
    coordinator: Any,
    async_add_entities: Callable[[list[Any]], None],
    build_entities: Callable[[AtmeexDevice], Iterable[Any]],
) -> None:
    """Add entities for current devices and discover newly available ones later."""
    known_unique_ids: set[str] = set()

    def _sync_entities() -> None:
        data = getattr(coordinator, "data", None) or {}
        device_map = data.get("device_map", {}) or {}
        new_entities: list[Any] = []

        for dev in device_map.values():
            for entity in build_entities(dev):
                unique_id = getattr(entity, "unique_id", None) or getattr(
                    entity, "_attr_unique_id", None
                )
                if unique_id is None:
                    continue
                unique_key = str(unique_id)
                if unique_key in known_unique_ids:
                    continue
                known_unique_ids.add(unique_key)
                new_entities.append(entity)

        if new_entities:
            async_add_entities(new_entities)

    _sync_entities()

    add_listener = getattr(coordinator, "async_add_listener", None)
    if not callable(add_listener):
        return

    remove_listener = add_listener(_sync_entities)
    async_on_unload = getattr(entry, "async_on_unload", None)
    if callable(async_on_unload):
        async_on_unload(remove_listener)
