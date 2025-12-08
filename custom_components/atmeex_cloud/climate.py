from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Callable, Awaitable

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
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.exceptions import HomeAssistantError
from .api import ApiError

from .const import DOMAIN, BRIZER_MODES
from . import AtmeexRuntimeData

_LOGGER = logging.getLogger(__name__)

# Доступные скорости вентилятора (строки — так их удобнее показывать в UI)
FAN_MODES = ["1", "2", "3", "4", "5", "6", "7"]

# Режимы заслонки / бризера
BRIZER_SWING_MODES = BRIZER_MODES

# Допустимые уровни целевой влажности (для «прилипания» слайдера)
HUM_ALLOWED = [0, 33, 66, 100]


def _quantize_humidity(val: int | float | None) -> int:
    """Привести влажность к ближайшему значению из 0/33/66/100."""
    if val is None:
        return 0
    v = max(0, min(100, int(round(val))))
    return min(HUM_ALLOWED, key=lambda x: abs(x - v))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Создание климат-сущностей для всех устройств интеграции.

    Достаём runtime_data, который был записан в entry.runtime_data в __init__.py,
    и по списку устройств из координатора создаём по одной сущности Climate
    на каждый бризер.
    """
    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator
    api = runtime.api

    entities: list[AtmeexClimateEntity] = []
    for dev in coordinator.data.get("devices", []):
        did = dev.get("id")
        if did is None:
            continue
        name = dev.get("name") or f"Device {did}"
        entities.append(
            AtmeexClimateEntity(
                coordinator=coordinator,
                api=api,
                entry_id=entry.entry_id,
                device_id=did,
                name=name,
                refresh_device_cb=runtime.refresh_device,
            )
        )

    if entities:
        async_add_entities(entities)


class AtmeexClimateEntity(CoordinatorEntity, ClimateEntity):
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

    def __init__(
        self,
        coordinator,
        api,
        entry_id: str | None,
        device_id: int | str,
        name: str,
        refresh_device_cb: Callable[[int | str], Awaitable[None]] | None = None,
    ) -> None:
        """Инициализация сущности.

        entry_id и refresh_device_cb важны только в реальном HA.
        В тестах могут быть None.
        """
        super().__init__(coordinator)
        self.api = api
        self._entry_id = entry_id
        self._device_id = device_id
        self._refresh_device_cb = refresh_device_cb
        self._attr_name = name
        self._attr_unique_id = f"{device_id}_climate"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, str(device_id))},
            "name": name,
            "manufacturer": "Atmeex",
        }

    # ---------- вспомогательные свойства ----------

    @property
    def _device_state(self) -> dict[str, Any]:
        """Удобный доступ к нормализованному состоянию устройства из координатора."""
        return self.coordinator.data.get("states", {}).get(str(self._device_id), {}) or {}

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

    # ---------- доступность сущности ----------

    @property
    def available(self) -> bool:
        """Считать сущность доступной, если устройство online."""
        return bool(self._device_state.get("online", True))

    # ---------- поддерживаемые возможности ----------

    @property
    def supported_features(self) -> int:
        """Вернуть битовую маску поддерживаемых функций."""
        features = self._base_supported
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
        """Текущая комнатная температура (по датчику), °C."""
        val = self._device_state.get("temp_room")  # деци-°C
        return (val / 10) if isinstance(val, (int, float)) else None

    @property
    def target_temperature(self) -> float:
        """Целевая температура, °C.

        Если цели нет, возвращаем текущую температуру, а если и её нет —
        дефолт 20.0 °C.
        """
        val = self._device_state.get("u_temp_room")  # деци-°C (цель)
        if isinstance(val, (int, float)):
            return val / 10
        cur = self.current_temperature
        return cur if isinstance(cur, (int, float)) else 20.0

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
        q = _quantize_humidity(humidity)
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
        """Текущая скорость вентилятора в виде строки 1..7."""
        speed = self._device_state.get("fan_speed")
        if isinstance(speed, (int, float)):
            speed = int(speed)
        return FAN_MODES[speed - 1] if speed in range(1, 8) else None

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Установить скорость вентилятора по выбранному режиму."""
        try:
            speed = int(fan_mode)
        except (ValueError, TypeError):
            _LOGGER.warning("Unsupported fan_mode: %s", fan_mode)
            return
        try:
            await self.api.set_fan_speed(self._device_id, speed)
        except ApiError as err:
            _LOGGER.error(
                "Failed to set fan speed for %s: %s", self._device_id, err
            )
            raise HomeAssistantError("Failed to set fan mode") from err
        await self._refresh()

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

    # ---------- дополнительные атрибуты для UI / отладки ----------

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Вернуть дополнительные атрибуты (температуры в °C и наличие увлажнителя)."""
        attrs = dict(self._device_state)
        tr = self._device_state.get("temp_room")
        ut = self._device_state.get("u_temp_room")
        if isinstance(tr, (int, float)):
            attrs["room_temp_c"] = round(tr / 10, 1)
        if isinstance(ut, (int, float)):
            attrs["target_temp_c"] = round(ut / 10, 1)
        attrs["has_humidifier"] = self._has_humidifier()

        # expose last_success_ts from coordinator data
        data = getattr(self.coordinator, "data", {}) or {}
        ts = data.get("last_success_ts")
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
