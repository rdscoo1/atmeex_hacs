from __future__ import annotations

import time
from datetime import datetime, timezone
import logging
from typing import Any, Callable, Awaitable
from .helpers import deci_to_c, quantize_humidity, HUM_ALLOWED
from .entity_base import AtmeexEntityMixin

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import (
    UnitOfTemperature,
    ATTR_TEMPERATURE,
    PRECISION_WHOLE,
)
from homeassistant.components.climate.const import (
    PRESET_NONE,
    PRESET_BOOST,
    PRESET_SLEEP,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.exceptions import HomeAssistantError
from .api import ApiError, AtmeexDevice

from .const import DOMAIN, BRIZER_MODES
from . import AtmeexRuntimeData

_LOGGER = logging.getLogger(__name__)

# Tolerance for pending command expiration (seconds)
PENDING_COMMAND_TTL = 5.0

# Доступные скорости вентилятора (строки — так их удобнее показывать в UI)
FAN_MODES = ["1", "2", "3", "4", "5", "6", "7"]

# Режимы заслонки / бризера
BRIZER_SWING_MODES = BRIZER_MODES


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Создание климат-сущностей для всех устройств интеграции"""
    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator
    api = runtime.api

    data = coordinator.data or {}
    device_map: dict[str, AtmeexDevice] = data.get("device_map", {})

    entities: list[AtmeexClimateEntity] = []
    for dev in device_map.values():
        entities.append(
            AtmeexClimateEntity(
                coordinator=coordinator,
                api=api,
                entry_id=entry.entry_id,
                device=dev,
                refresh_device_cb=runtime.refresh_device,
                runtime=runtime,
            )
        )

    if entities:
        async_add_entities(entities)


class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
    """Климатическая сущность для бризера Atmeex.

    Управляет:
    * целевой температурой;
    * скоростью вентилятора (1..7);
    * режимом заслонки (swing / brizer mode);
    * целевой влажностью (если есть увлажнитель).
    """

    # Базовый набор возможностей (без учёта увлажнителя)
    _base_supported = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
    )

    _attr_hvac_modes = [HVACMode.FAN_ONLY, HVACMode.OFF]
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_WHOLE
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 10
    _attr_max_temp = 30
    _attr_fan_modes = FAN_MODES
    _attr_swing_modes = BRIZER_SWING_MODES
    _attr_icon = "mdi:air-purifier"
    _attr_has_entity_name = True
    _attr_translation_key = "breezer"
    _attr_preset_modes = [PRESET_NONE, PRESET_BOOST, PRESET_SLEEP]
    _attr_preset_mode = PRESET_NONE

    def __init__(
        self,
        coordinator,
        api,
        entry_id: str | None,
        device: AtmeexDevice,
        refresh_device_cb: Callable[[int | str], Awaitable[None]] | None = None,
        runtime: AtmeexRuntimeData | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.api = api
        self._entry_id = entry_id
        self._device_meta = device
        self._device_id = device.id
        self._refresh_device_cb = refresh_device_cb
        self._runtime = runtime

        self._attr_name = device.name
        self._attr_unique_id = f"{device.id}_climate"
        self._saved_fan_mode: str | None = None
        self._saved_target_temp: float | None = None
        self._is_boost = False

    # ---------- вспомогательные свойства ----------

    def _has_humidifier(self) -> bool:
        """Есть ли у устройства увлажнитель (по наличию hum_stg)."""
        stg = self._device_state.get("hum_stg")
        return isinstance(stg, (int, float)) or ("hum_stg" in self._device_state)

    async def _refresh(self) -> None:
        """Запросить актуальные данные по одному устройству через refresh_device.

        В проде refresh_device приходит из runtime_data и ходит в API.
        В тестах этот метод часто заменяется на AsyncMock().
        """
        if callable(self._refresh_device_cb):
            await self._refresh_device_cb(self._device_id)
        else:
            await self.coordinator.async_request_refresh()
        
    @property
    def boost_fan_mode(self) -> str:
        return FAN_MODES[-1]  # "7"

    @property
    def sleep_max_fan_mode(self) -> str:
        return "2"

    # ---------- доступность сущности ----------

    @property
    def available(self) -> bool:
        """Считать сущность доступной, если устройство online."""
        return bool(self._device_state.get("online", True))

    # ---------- поддерживаемые возможности ----------

    @property
    def supported_features(self) -> int:
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.SWING_MODE
            | ClimateEntityFeature.PRESET_MODE
        )
        if self._has_humidifier():
            features |= ClimateEntityFeature.TARGET_HUMIDITY
        return features
    
    # ---------- режим работы (HVAC) ----------

    @property
    def hvac_mode(self) -> HVACMode:
        """Возвращает текущий режим: FAN_ONLY или OFF."""
        return HVACMode.FAN_ONLY if bool(self._device_state.get("pwr_on")) else HVACMode.OFF

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Включить/выключить устройство по смене режима HVAC."""
        try:
            await self.api.set_power(self._device_id, hvac_mode != HVACMode.OFF)
        except ApiError as err:
            _LOGGER.error("Failed to set HVAC mode for %s: %s", self._device_id, err)
            raise HomeAssistantError("Failed to set HVAC mode") from err
        await self._refresh()

    # ---------- температура ----------

    @property
    def current_temperature(self) -> float | None:
        return deci_to_c(self._device_state.get("temp_room"))

    @property
    def target_temperature(self) -> float:
        v = self._device_state.get("u_temp_room")
        t = deci_to_c(v)
        if t is not None:
            return t
        return self.current_temperature or 20.0

    async def async_set_temperature(self, **kwargs) -> None:
        """Установить целевую температуру.

        Если устройство выключено, сначала включаем питание.
        Затем отправляем целевое значение в API и обновляем состояние.
        """
        t = kwargs.get(ATTR_TEMPERATURE)
        if t is None:
            return
    
        # клампинг температуры к min/max
        try:
            t_float = float(t)
        except (ValueError, TypeError):
            _LOGGER.warning("Invalid temperature value: %s", t)
            return

        t_clamped = max(self._attr_min_temp, min(self._attr_max_temp, t_float))

        try:
            if not bool(self._device_state.get("pwr_on")):
                await self.api.set_power(self._device_id, True)
            await self.api.set_target_temperature(self._device_id, t_clamped)
        except ApiError as err:
            _LOGGER.error(
                "Failed to set target temperature for %s: %s", self._device_id, err
            )
            raise HomeAssistantError("Failed to set temperature") from err

        await self._refresh()

    # ---------- влажность ----------

    @property
    def current_humidity(self) -> int | None:
        """Текущая влажность в помещении, %."""
        val = self._device_state.get("hum_room")
        return int(val) if isinstance(val, (int, float)) else None

    @property
    def target_humidity(self) -> int | None:
        """Целевая влажность — одно из 0/33/66/100.

        Значение вычисляется по текущей ступени hum_stg (0..3).
        Если увлажнителя нет, возвращаем None.
        """
        if not self._has_humidifier():
            return None
        stg = self._device_state.get("hum_stg")
        if not isinstance(stg, (int, float)):
            stg = 0
        stg = max(0, min(3, int(stg)))
        return HUM_ALLOWED[stg]

    async def async_set_humidity(self, humidity: int) -> None:
        """Установить целевую влажность.

        Любое значение квантуется в 0/33/66/100, затем переводится
        в ступень 0..3 и отправляется в API.
        """
        if not self._has_humidifier():
            return
        q = quantize_humidity(humidity)
        stage = HUM_ALLOWED.index(q)
        try:
            await self.api.set_humid_stage(self._device_id, stage)
        except ApiError as err:
            _LOGGER.error(
                "Failed to set humidity stage for %s: %s", self._device_id, err
            )
            raise HomeAssistantError("Failed to set humidity") from err
        await self._refresh()

    # ---------- вентилятор ----------

    @property
    def fan_mode(self) -> str | None:
        """Текущая скорость вентилятора в виде строки 1..7.
        
        Uses pending command value if a recent command was sent and not yet
        confirmed by the device, to prevent UI regression during rapid changes.
        """
        confirmed_speed = self._device_state.get("fan_speed")
        
        # Check if there's a pending fan_speed command that should take precedence
        if self._runtime is not None:
            pending = self._runtime.get_pending(self._device_id, "fan_speed")
            if pending is not None:
                age = time.monotonic() - pending.timestamp
                if age <= PENDING_COMMAND_TTL:
                    # Use pending value if not expired and not yet confirmed
                    if pending.value != confirmed_speed:
                        _LOGGER.debug(
                            "Climate: Using pending fan_speed=%s instead of confirmed=%s (age=%.1fs)",
                            pending.value, confirmed_speed, age
                        )
                        speed = pending.value
                        if isinstance(speed, (int, float)):
                            speed = int(speed)
                        return FAN_MODES[speed - 1] if speed in range(1, 8) else None
                    else:
                        # Device confirmed our value, clear pending
                        self._runtime.clear_pending(self._device_id, "fan_speed")
                else:
                    # Pending expired, clear it
                    self._runtime.clear_pending(self._device_id, "fan_speed")
        
        if isinstance(confirmed_speed, (int, float)):
            confirmed_speed = int(confirmed_speed)
        return FAN_MODES[confirmed_speed - 1] if confirmed_speed in range(1, 8) else None

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Установить скорость вентилятора по выбранному режиму.
        
        Uses device lock to serialize operations and tracks pending command
        to prevent stale responses from overwriting newer state.
        """
        try:
            speed = int(fan_mode)
        except (ValueError, TypeError):
            _LOGGER.warning("Unsupported fan_mode: %s", fan_mode)
            return
        
        # Record pending command BEFORE acquiring lock
        if self._runtime is not None:
            self._runtime.set_pending(self._device_id, "fan_speed", speed)
        
        _LOGGER.debug(
            "Climate: Setting fan speed: device=%s speed=%s",
            self._device_id, speed
        )
        
        # Use device lock to serialize set+refresh operations
        lock = self._runtime.get_device_lock(self._device_id) if self._runtime else None
        
        async def _do_set_and_refresh():
            try:
                await self.api.set_fan_speed(self._device_id, speed)
            except ApiError as err:
                _LOGGER.error(
                    "Failed to set fan speed for %s: %s", self._device_id, err
                )
                # Clear pending on error
                if self._runtime is not None:
                    self._runtime.clear_pending(self._device_id, "fan_speed")
                raise HomeAssistantError("Failed to set fan mode") from err
            
            await self._refresh()
            
            _LOGGER.debug(
                "Climate: Fan speed set complete: device=%s speed=%s",
                self._device_id, speed
            )
        
        if lock is not None:
            async with lock:
                await _do_set_and_refresh()
        else:
            await _do_set_and_refresh()

    # ---------- режим заслонки (swing) ----------

    @property
    def swing_mode(self) -> str | None:
        """Текущий режим заслонки / бризера."""
        pos = self._device_state.get("damp_pos")
        if isinstance(pos, int) and 0 <= pos <= 3:
            return BRIZER_SWING_MODES[pos]
        return None

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Установить режим заслонки / бризера."""
        if swing_mode not in BRIZER_SWING_MODES:
            _LOGGER.warning("Unsupported swing_mode: %s", swing_mode)
            return
        try:
            await self.api.set_brizer_mode(
                self._device_id, BRIZER_SWING_MODES.index(swing_mode)
            )
        except ApiError as err:
            _LOGGER.error(
                "Failed to set swing mode for %s: %s", self._device_id, err
            )
            raise HomeAssistantError("Failed to set swing mode") from err
        await self._refresh()

    # ---------- установка пресетов ----------

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        old = self.preset_mode

        actions: list[tuple[Callable, dict]] = []

        # Переход в SLEEP
        if preset_mode == PRESET_SLEEP and old != PRESET_SLEEP:
            if self._saved_fan_mode is None and self.fan_mode is not None:
                self._saved_fan_mode = self.fan_mode
            target = min(int(self.fan_mode or "1"), int(self.sleep_max_fan_mode))
            actions.append((self.async_set_fan_mode, {"fan_mode": str(target)}))

        # Переход в BOOST
        if preset_mode == PRESET_BOOST and old != PRESET_BOOST:
            self._is_boost = True
            if self._saved_fan_mode is None and self.fan_mode is not None:
                self._saved_fan_mode = self.fan_mode
            actions.append((self.async_set_fan_mode, {"fan_mode": self.boost_fan_mode}))

        # Выход из BOOST/SLEEP в NORMAL
        if old in (PRESET_BOOST, PRESET_SLEEP) and preset_mode == PRESET_NONE:
            if self._saved_fan_mode is not None:
                actions.append(
                    (self.async_set_fan_mode, {"fan_mode": self._saved_fan_mode})
                )
                self._saved_fan_mode = None
            self._is_boost = False

        for func, kwargs in actions:
            await func(**kwargs)

        self._attr_preset_mode = preset_mode
        await self._refresh()
        self.async_write_ha_state()

    # ---------- дополнительные атрибуты для UI / отладки ----------

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Вернуть дополнительные атрибуты (температуры в °C и наличие увлажнителя)."""
        attrs = dict(self._device_state)
        room = deci_to_c(self._device_state.get("temp_room"))
        target = deci_to_c(self._device_state.get("u_temp_room"))
        if room is not None:
            attrs["room_temp_c"] = round(room, 1)
        if target is not None:
            attrs["target_temp_c"] = round(target, 1)
        attrs["has_humidifier"] = self._has_humidifier()

        # expose last_success_ts from coordinator data
        data = getattr(self.coordinator, "data", {}) or {}
        ts = getattr(self.coordinator, "last_success_ts", None)
        avg = data.get("avg_latency_ms")
        if isinstance(avg, (int, float)):
            attrs["avg_latency_ms"] = avg
            attrs["last_success_ts"] = ts
            try:
                attrs["last_success_utc"] = datetime.fromtimestamp(
                    ts, tz=timezone.utc
                ).isoformat()
            except Exception:  # pragma: no cover - defensive only
                pass

        return attrs
