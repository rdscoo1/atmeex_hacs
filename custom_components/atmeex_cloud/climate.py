from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Callable, Awaitable
from .helpers import deci_to_c, quantize_humidity, HUM_ALLOWED
from .entity_base import (
    AtmeexEntityMixin,
    setup_dynamic_device_entities,
    supports_humidifier,
)

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import (
    UnitOfTemperature,
    ATTR_TEMPERATURE,
    PRECISION_HALVES,
)
from homeassistant.components.climate.const import (
    PRESET_NONE,
    PRESET_BOOST,
    PRESET_SLEEP,
)

PRESET_AUTO = "auto"
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import async_get_current_platform
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.exceptions import HomeAssistantError
from .api import ApiError, AtmeexDevice

from .const import BREEZER_MODES
from . import AtmeexRuntimeData

_LOGGER = logging.getLogger(__name__)

# Tolerance for pending command expiration (seconds)
# Increased to 8s because API condition updates are slow
PENDING_COMMAND_TTL = 8.0

# Доступные скорости вентилятора (строки — так их удобнее показывать в UI)
FAN_MODES = ["1", "2", "3", "4", "5", "6", "7"]

# Режимы заслонки / бризера
BREEZER_SWING_MODES = BREEZER_MODES


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Создание климат-сущностей для всех устройств интеграции"""
    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator
    api = runtime.api

    def _build_entities(dev: AtmeexDevice) -> list[AtmeexClimateEntity]:
        return [
            AtmeexClimateEntity(
                coordinator=coordinator,
                api=api,
                entry_id=entry.entry_id,
                device=dev,
                refresh_device_cb=runtime.refresh_device,
                runtime=runtime,
            )
        ]

    setup_dynamic_device_entities(
        entry=entry,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        build_entities=_build_entities,
    )

    platform = async_get_current_platform()
    platform.async_register_entity_service(
        "set_breezer_mode",
        {vol.Required("mode"): vol.In(BREEZER_MODES)},
        "async_set_breezer_mode",
    )
    platform.async_register_entity_service(
        "set_humidifier_stage",
        {vol.Required("stage"): vol.All(vol.Coerce(int), vol.Range(min=0, max=3))},
        "async_set_humidifier_stage",
    )


class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
    """Климатическая сущность для бризера Atmeex.

    Управляет:
    * целевой температурой;
    * скоростью вентилятора (1..7);
    * режимом заслонки (swing / breezer mode);
    * целевой влажностью (если есть увлажнитель).
    """

    _attr_hvac_modes = [HVACMode.FAN_ONLY, HVACMode.OFF]
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_HALVES
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 10
    _attr_max_temp = 30
    _attr_fan_modes = FAN_MODES
    _attr_swing_modes = BREEZER_SWING_MODES
    _attr_icon = "mdi:air-purifier"
    _attr_has_entity_name = True
    _attr_translation_key = "breezer"
    _attr_min_humidity = 0
    _attr_max_humidity = 100
    _attr_preset_modes = [PRESET_NONE, PRESET_AUTO, PRESET_BOOST, PRESET_SLEEP]

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

        self._attr_unique_id = f"{device.id}_climate"
        self._saved_fan_mode: str | None = None
        self._is_boost = False
        self._local_preset: str | None = None  # tracks BOOST (client-side only)

    # ---------- вспомогательные свойства ----------

    def _has_humidifier(self) -> bool:
        """Есть ли у устройства увлажнитель (по наличию hum_stg)."""
        return supports_humidifier(self._device_state)

    @property
    def boost_fan_mode(self) -> str:
        return FAN_MODES[-1]  # "7"

    @property
    def sleep_max_fan_mode(self) -> str:
        return "2"

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
        """Возвращает текущий режим: FAN_ONLY или OFF.
        
        Uses pending command value if a recent command was sent and not yet
        confirmed by the device, to prevent UI regression.
        """
        confirmed_pwr = bool(self._device_state.get("pwr_on"))
        effective_pwr = self._state_with_pending(
            "pwr_on",
            confirmed_pwr,
            tolerance=PENDING_COMMAND_TTL,
        )
        if effective_pwr != confirmed_pwr:
            _LOGGER.debug(
                "Climate: using pending pwr_on=%s instead of confirmed=%s",
                effective_pwr,
                confirmed_pwr,
            )
        return HVACMode.FAN_ONLY if bool(effective_pwr) else HVACMode.OFF

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        power_on = hvac_mode != HVACMode.OFF
        await self._execute_command(
            self.api.set_power(self._device_id, power_on),
            pending_attr="pwr_on",
            pending_value=power_on,
            error_message="Failed to set HVAC mode",
        )

    # ---------- температура ----------

    @property
    def current_temperature(self) -> float | None:
        return deci_to_c(self._device_state.get("temp_room"))

    @property
    def target_temperature(self) -> float | None:
        v = self._device_state.get("u_temp_room")
        t = deci_to_c(v)
        if t is not None and self._attr_min_temp <= t <= self._attr_max_temp:
            return t
        return None

    async def async_set_temperature(self, **kwargs) -> None:
        t = kwargs.get(ATTR_TEMPERATURE)
        if t is None:
            return
        try:
            t_float = float(t)
        except (ValueError, TypeError):
            _LOGGER.warning("Invalid temperature value: %s", t)
            return
        t_clamped = max(self._attr_min_temp, min(self._attr_max_temp, t_float))

        async def _set_temp():
            if not bool(self._device_state.get("pwr_on")):
                await self.api.set_power(self._device_id, True)
            await self.api.set_target_temperature(self._device_id, t_clamped)

        await self._execute_command(
            _set_temp(),
            error_message="Failed to set temperature",
        )

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
        if not self._has_humidifier():
            return
        q = quantize_humidity(humidity)
        stage = HUM_ALLOWED.index(q)
        await self._execute_command(
            self.api.set_humid_stage(self._device_id, stage),
            error_message="Failed to set humidity",
        )

    # ---------- вентилятор ----------

    @property
    def fan_mode(self) -> str | None:
        """Текущая скорость вентилятора в виде строки 1..7.
        
        Uses pending command value if a recent command was sent and not yet
        confirmed by the device, to prevent UI regression during rapid changes.
        """
        confirmed_speed_raw = self._device_state.get("fan_speed")
        confirmed_speed = int(confirmed_speed_raw) if isinstance(
            confirmed_speed_raw, (int, float)
        ) else None
        effective_speed = self._state_with_pending(
            "fan_speed",
            confirmed_speed,
            tolerance=PENDING_COMMAND_TTL,
        )
        if effective_speed != confirmed_speed:
            _LOGGER.debug(
                "Climate: using pending speed=%s instead of confirmed=%s",
                effective_speed,
                confirmed_speed,
            )

        if isinstance(effective_speed, (int, float)):
            speed = int(effective_speed)
            return str(speed) if speed in range(1, 8) else None
        return None

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        try:
            speed = int(fan_mode)
        except (ValueError, TypeError):
            _LOGGER.warning("Unsupported fan_mode: %s", fan_mode)
            return
        await self._execute_command(
            self.api.set_fan_speed(self._device_id, speed),
            pending_attr="fan_speed",
            pending_value=speed,
            error_message="Failed to set fan mode",
        )

    # ---------- режим заслонки (swing) ----------

    @property
    def swing_mode(self) -> str | None:
        """Текущий режим заслонки / бризера."""
        pos = self._device_state.get("damp_pos")
        if isinstance(pos, int) and 0 <= pos <= 3:
            return BREEZER_SWING_MODES[pos]
        return None

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        if swing_mode not in BREEZER_SWING_MODES:
            _LOGGER.warning("Unsupported swing_mode: %s", swing_mode)
            return
        await self._execute_command(
            self.api.set_breezer_mode(self._device_id, BREEZER_SWING_MODES.index(swing_mode)),
            error_message="Failed to set swing mode",
        )

    async def async_set_breezer_mode(self, mode: str) -> None:
        """Service handler: set damper/breezer mode by name."""
        await self.async_set_swing_mode(mode)

    async def async_set_humidifier_stage(self, stage: int) -> None:
        if not self._has_humidifier():
            return
        stage = max(0, min(3, int(stage)))
        await self._execute_command(
            self.api.set_humid_stage(self._device_id, stage),
            error_message="Failed to set humidifier stage",
        )

    # ---------- пресеты ----------

    @property
    def preset_mode(self) -> str:
        """Return current preset based on device state and local overrides.

        PRESET_AUTO and PRESET_SLEEP are read from the API (u_auto / u_night).
        PRESET_BOOST is client-side only (max fan speed override).
        """
        if self._is_boost:
            return PRESET_BOOST

        st = self._device_state
        if st.get("u_night"):
            return PRESET_SLEEP
        if st.get("u_auto"):
            return PRESET_AUTO
        return PRESET_NONE

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        old = self.preset_mode

        # --- exit current mode first ---
        if old == PRESET_BOOST and preset_mode != PRESET_BOOST:
            self._is_boost = False
            if self._saved_fan_mode is not None:
                await self.async_set_fan_mode(self._saved_fan_mode)
                self._saved_fan_mode = None
        if old == PRESET_SLEEP and preset_mode != PRESET_SLEEP:
            try:
                await self.api.set_sleep_mode(self._device_id, False)
            except ApiError as err:
                _LOGGER.error("Failed to disable sleep mode: %s", err)
            if self._saved_fan_mode is not None:
                await self.async_set_fan_mode(self._saved_fan_mode)
                self._saved_fan_mode = None
        if old == PRESET_AUTO and preset_mode != PRESET_AUTO:
            try:
                await self.api.set_auto_mode(self._device_id, False)
            except ApiError as err:
                _LOGGER.error("Failed to disable auto mode: %s", err)

        # --- enter new mode ---
        if preset_mode == PRESET_AUTO:
            try:
                await self.api.set_auto_mode(self._device_id, True)
            except ApiError as err:
                _LOGGER.error("Failed to enable auto mode: %s", err)
                raise HomeAssistantError("Failed to set auto mode") from err

        elif preset_mode == PRESET_SLEEP:
            if self._saved_fan_mode is None and self.fan_mode is not None:
                self._saved_fan_mode = self.fan_mode
            target = min(int(self.fan_mode or "1"), int(self.sleep_max_fan_mode))
            try:
                await self.api.set_sleep_mode(self._device_id, True)
            except ApiError as err:
                _LOGGER.error("Failed to enable sleep mode: %s", err)
                raise HomeAssistantError("Failed to set sleep mode") from err
            await self.async_set_fan_mode(str(target))

        elif preset_mode == PRESET_BOOST:
            self._is_boost = True
            if self._saved_fan_mode is None and self.fan_mode is not None:
                self._saved_fan_mode = self.fan_mode
            await self.async_set_fan_mode(self.boost_fan_mode)

        # PRESET_NONE: modes already disabled above

        self._local_preset = preset_mode if preset_mode == PRESET_BOOST else None
        await self._refresh()
        self.async_write_ha_state()

    # ---------- дополнительные атрибуты для UI / отладки ----------

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Вернуть дополнительные атрибуты (температуры в °C и наличие увлажнителя)."""
        attrs: dict[str, Any] = {}
        state = self._device_state
        room = deci_to_c(state.get("temp_room"))
        target = deci_to_c(state.get("u_temp_room"))
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
