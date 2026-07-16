from __future__ import annotations

from datetime import datetime, timezone
import logging
import math
from typing import Any, Callable, Awaitable
from .helpers import c_to_deci, deci_to_c, quantize_humidity, humidity_to_stage, HUM_ALLOWED
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import async_get_current_platform
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.exceptions import ServiceValidationError
from .api import AtmeexDevice

from .const import BREEZER_MODES
from . import AtmeexRuntimeData

_LOGGER = logging.getLogger(__name__)

# Доступные скорости вентилятора (строки — так их удобнее показывать в UI)
FAN_MODES = ["1", "2", "3", "4", "5", "6", "7"]
_PRESET_RESTORE_FAN = "preset_restore_fan"

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

    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.FAN_ONLY, HVACMode.OFF]
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
        self._unconfirmed_restore_fan_mode: str | None = None
        self._unconfirmed_restore_token: object | None = None
        self._is_boost = False
        self._local_preset: str | None = None  # tracks BOOST (client-side only)
        self._last_heat_temp: float | None = None  # last known valid target °C; lost on restart

    # ---------- вспомогательные свойства ----------

    def _has_humidifier(self) -> bool:
        """Есть ли у устройства увлажнитель (по наличию hum_stg)."""
        return supports_humidifier(self._device_state)

    def _invalid_command_value(
        self,
        field: str,
        value: Any,
    ) -> ServiceValidationError:
        """Build a translated validation error without trusting value.__str__."""
        try:
            return self._invalid_value(field, value)
        except Exception:
            return self._invalid_value(
                field,
                f"<{type(value).__name__} outside supported range>",
            )

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
        """Return current HVAC mode: HEAT, FAN_ONLY, or OFF.

        Uses pending command values so the UI doesn't flicker after a set command.
        """
        confirmed_pwr = bool(self._device_state.get("pwr_on"))
        effective_pwr = self._state_with_pending(
            "pwr_on",
            confirmed_pwr,
        )
        if not bool(effective_pwr):
            return HVACMode.OFF

        # Device is on — determine HEAT vs FAN_ONLY
        confirmed_temp = self._device_state.get("u_temp_room")
        effective_temp = self._state_with_pending(
            "u_temp_room",
            confirmed_temp,
        )
        temp_valid = isinstance(effective_temp, (int, float)) and effective_temp >= 100
        damp_pos = self._device_state.get("damp_pos")
        recirculation = damp_pos == 1
        if temp_valid and not recirculation:
            return HVACMode.HEAT
        return HVACMode.FAN_ONLY

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in self.hvac_modes:
            raise self._invalid_command_value("hvac_mode", hvac_mode)

        if hvac_mode == HVACMode.OFF:
            async def operation() -> None:
                await self.api.set_power(self._device_id, False)

            await self._execute_command(
                operation,
                pending={"pwr_on": False},
                translation_placeholders={"action": "turn off climate control"},
            )
            return

        if hvac_mode == HVACMode.FAN_ONLY:
            async def operation() -> None:
                await self.api.set_power(self._device_id, True)
                await self.api.set_heater_off(self._device_id)

            await self._execute_command(
                operation,
                pending={"pwr_on": True, "u_temp_room": -1000},
                translation_placeholders={"action": "enter fan-only mode"},
            )
            return

        target_c = self._resolve_heat_target()

        async def operation() -> None:
            await self.api.set_power(self._device_id, True)
            await self.api.set_target_temperature(self._device_id, target_c)

        await self._execute_command(
            operation,
            pending={"pwr_on": True, "u_temp_room": c_to_deci(target_c)},
            translation_placeholders={"action": "enter heat mode"},
        )

    # ---------- температура ----------

    @property
    def current_temperature(self) -> float | None:
        return deci_to_c(self._device_state.get("temp_room"))

    @property
    def target_temperature(self) -> float | None:
        target = deci_to_c(self._device_state.get("u_temp_room"))
        if (
            target is not None
            and self._attr_min_temp <= target <= self._attr_max_temp
        ):
            return target
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._remember_confirmed_heat_target()
        super()._handle_coordinator_update()

    def _remember_confirmed_heat_target(self) -> None:
        """Track the last confirmed valid heat target for heat-mode recovery."""
        target = deci_to_c(self._device_state.get("u_temp_room"))
        if (
            target is not None
            and self._attr_min_temp <= target <= self._attr_max_temp
        ):
            self._last_heat_temp = target

    def _resolve_heat_target(self) -> float:
        """Return the best target temperature for entering HEAT mode.

        Priority: current u_temp_room → _last_heat_temp → 20.0 default.
        """
        v = self._device_state.get("u_temp_room")
        if isinstance(v, (int, float)) and 100 <= v <= 300:
            return v / 10.0
        if self._last_heat_temp is not None:
            return self._last_heat_temp
        return 20.0

    async def async_set_temperature(self, **kwargs) -> None:
        value = kwargs.get(ATTR_TEMPERATURE)
        if value is None or isinstance(value, bool):
            raise self._invalid_command_value("temperature", value)
        try:
            target = float(value)
        except (TypeError, ValueError, OverflowError) as err:
            raise self._invalid_command_value("temperature", value) from err
        if (
            not math.isfinite(target)
            or not self._attr_min_temp <= target <= self._attr_max_temp
        ):
            raise self._invalid_command_value("temperature", value)

        async def operation() -> None:
            await self.api.set_power(self._device_id, True)
            await self.api.set_target_temperature(self._device_id, target)

        await self._execute_command(
            operation,
            pending={"pwr_on": True, "u_temp_room": c_to_deci(target)},
            translation_placeholders={"action": "set target temperature"},
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
            raise self._unsupported_feature("humidifier")
        if (
            isinstance(humidity, bool)
            or not isinstance(humidity, (int, float))
            or not 0 <= humidity <= 100
            or not math.isfinite(float(humidity))
        ):
            raise self._invalid_command_value("humidity", humidity)
        stage = humidity_to_stage(humidity)

        async def operation() -> None:
            await self.api.set_humid_stage(self._device_id, stage)

        await self._execute_command(
            operation,
            pending={"hum_stg": stage},
            translation_placeholders={"action": "set target humidity"},
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
        if not isinstance(fan_mode, str) or fan_mode not in FAN_MODES:
            raise self._invalid_command_value("fan_mode", fan_mode)
        speed = int(fan_mode)
        restore_intent_token = object()
        tracks_manual_baseline: bool | None = None

        async def operation() -> None:
            nonlocal tracks_manual_baseline
            await self.api.set_fan_speed(self._device_id, speed)
            tracks_manual_baseline = self.preset_mode not in {
                PRESET_SLEEP,
                PRESET_BOOST,
            }
            if tracks_manual_baseline:
                self._unconfirmed_restore_fan_mode = fan_mode
                self._unconfirmed_restore_token = restore_intent_token

        confirmation_success = await self._execute_command(
            operation,
            pending={"fan_speed": speed},
            translation_placeholders={"action": "set climate fan mode"},
        )
        if (
            tracks_manual_baseline
            and confirmation_success
            and self._unconfirmed_restore_token is restore_intent_token
        ):
            self._unconfirmed_restore_fan_mode = None
            self._unconfirmed_restore_token = None

    # ---------- режим заслонки (swing) ----------

    @property
    def swing_mode(self) -> str | None:
        """Текущий режим заслонки / бризера."""
        pos = self._device_state.get("damp_pos")
        if isinstance(pos, int) and 0 <= pos <= 3:
            return BREEZER_SWING_MODES[pos]
        return None

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        if (
            not isinstance(swing_mode, str)
            or swing_mode not in BREEZER_SWING_MODES
        ):
            raise self._invalid_command_value("swing_mode", swing_mode)
        mode = BREEZER_SWING_MODES.index(swing_mode)
        power = mode != 3
        damper = 0 if mode == 3 else mode

        async def operation() -> None:
            await self.api.set_breezer_mode(self._device_id, mode)

        await self._execute_command(
            operation,
            pending={"pwr_on": power, "damp_pos": damper},
            translation_placeholders={"action": "set breezer mode"},
        )

    async def async_set_breezer_mode(self, mode: str) -> None:
        """Service handler: set damper/breezer mode by name."""
        await self.async_set_swing_mode(mode)

    async def async_set_humidifier_stage(self, stage: int) -> None:
        if not self._has_humidifier():
            raise self._unsupported_feature("humidifier")
        if (
            isinstance(stage, bool)
            or not isinstance(stage, int)
            or not 0 <= stage <= 3
        ):
            raise self._invalid_command_value("humidifier_stage", stage)

        async def operation() -> None:
            await self.api.set_humid_stage(self._device_id, stage)

        await self._execute_command(
            operation,
            pending={"hum_stg": stage},
            translation_placeholders={"action": "set humidifier stage"},
        )

    # ---------- пресеты ----------

    @property
    def preset_mode(self) -> str:
        """Return the newest local or cloud-backed preset."""
        local_preset = self._state_with_pending(
            "local_preset", self._local_preset
        )
        if local_preset == PRESET_BOOST:
            return PRESET_BOOST
        night = bool(
            self._state_with_pending(
                "u_night", bool(self._device_state.get("u_night"))
            )
        )
        auto = bool(
            self._state_with_pending(
                "u_auto", bool(self._device_state.get("u_auto"))
            )
        )
        if night:
            return PRESET_SLEEP
        if auto:
            return PRESET_AUTO
        return PRESET_NONE

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if (
            not isinstance(preset_mode, str)
            or preset_mode not in self.preset_modes
        ):
            raise self._invalid_command_value("preset_mode", preset_mode)

        # Capture predecessor intent before this generation is installed. It
        # is used only to carry the original fan speed across queued preset
        # transitions; the cloud flags below are always established explicitly.
        previous_preset = self.preset_mode
        runtime = getattr(self, "_runtime", None)
        executor = getattr(runtime, "command_executor", None) or getattr(
            self,
            "_fallback_command_executor",
            None,
        )
        restore_pending = (
            executor.get_pending(self._device_id, _PRESET_RESTORE_FAN)
            if executor is not None
            else None
        )
        queued_restore = (
            restore_pending.value
            if restore_pending is not None
            and isinstance(restore_pending.value, str)
            and restore_pending.value in FAN_MODES
            else None
        )
        saved_restore = (
            self._saved_fan_mode
            if isinstance(self._saved_fan_mode, str)
            and self._saved_fan_mode in FAN_MODES
            else None
        )
        unconfirmed_restore = (
            self._unconfirmed_restore_fan_mode
            if isinstance(self._unconfirmed_restore_fan_mode, str)
            and self._unconfirmed_restore_fan_mode in FAN_MODES
            else None
        )
        current_fan = self.fan_mode
        restore_fan = (
            queued_restore
            or unconfirmed_restore
            or (
                saved_restore
                if previous_preset in {PRESET_SLEEP, PRESET_BOOST}
                else None
            )
            or current_fan
            or FAN_MODES[0]
        )
        needs_restore = (
            queued_restore is not None
            or unconfirmed_restore is not None
            or previous_preset in {PRESET_SLEEP, PRESET_BOOST}
        )

        fan_target: int | None = None
        saved_after: str | None = None
        if preset_mode == PRESET_SLEEP:
            saved_after = restore_fan
            fan_target = min(int(restore_fan), int(self.sleep_max_fan_mode))
        elif preset_mode == PRESET_BOOST:
            saved_after = restore_fan
            fan_target = int(self.boost_fan_mode)
        elif needs_restore:
            fan_target = int(restore_fan)

        pending: dict[str, Any] = {
            "u_auto": preset_mode == PRESET_AUTO,
            "u_night": preset_mode == PRESET_SLEEP,
            "local_preset": (
                PRESET_BOOST if preset_mode == PRESET_BOOST else None
            ),
        }
        carries_restore_intent = (
            needs_restore or preset_mode in {PRESET_SLEEP, PRESET_BOOST}
        )
        restore_intent_token = object()
        if carries_restore_intent:
            pending[_PRESET_RESTORE_FAN] = restore_fan
        if fan_target is not None:
            pending["fan_speed"] = fan_target

        async def operation() -> None:
            if preset_mode == PRESET_AUTO:
                await self.api.set_sleep_mode(self._device_id, False)
                await self.api.set_auto_mode(self._device_id, True)
            elif preset_mode == PRESET_SLEEP:
                await self.api.set_auto_mode(self._device_id, False)
                await self.api.set_sleep_mode(self._device_id, True)
            else:
                await self.api.set_auto_mode(self._device_id, False)
                await self.api.set_sleep_mode(self._device_id, False)

            if fan_target is not None:
                await self.api.set_fan_speed(self._device_id, fan_target)

            # Commit all local state together, after the final remote write and
            # without another await. Failure/cancellation before this point
            # leaves the previous local transition intact.
            self._saved_fan_mode = saved_after
            if carries_restore_intent:
                self._unconfirmed_restore_fan_mode = restore_fan
                self._unconfirmed_restore_token = restore_intent_token
            self._is_boost = preset_mode == PRESET_BOOST
            self._local_preset = (
                PRESET_BOOST if preset_mode == PRESET_BOOST else None
            )

        confirmation_success = await self._execute_command(
            operation,
            pending=pending,
            translation_placeholders={"action": "set climate preset"},
        )
        if carries_restore_intent:
            executor = getattr(runtime, "command_executor", None) or getattr(
                self,
                "_fallback_command_executor",
                None,
            )
            if executor is not None:
                executor.confirm(
                    self._device_id,
                    _PRESET_RESTORE_FAN,
                    restore_fan,
                )
            if (
                confirmation_success
                and self._unconfirmed_restore_token is restore_intent_token
            ):
                self._unconfirmed_restore_fan_mode = None
                self._unconfirmed_restore_token = None
        if self.hass is not None:
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
