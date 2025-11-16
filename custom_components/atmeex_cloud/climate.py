from __future__ import annotations

import logging
from datetime import datetime, timezone 
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

from .const import DOMAIN
from . import AtmeexRuntimeData

_LOGGER = logging.getLogger(__name__)

FAN_MODES = ["1", "2", "3", "4", "5", "6", "7"]
BRIZER_SWING_MODES = [
    "приточная вентиляция",  # 0
    "рециркуляция",          # 1
    "смешанный режим",       # 2
    "приточный клапан",      # 3
]

# Допустимые уровни целевой влажности (для «прилипания» слайдера)
HUM_ALLOWED = [0, 33, 66, 100]


def _quantize_humidity(val: int | float | None) -> int:
    """Ближайшее из 0/33/66/100."""
    if val is None:
        return 0
    v = max(0, min(100, int(round(val))))
    return min(HUM_ALLOWED, key=lambda x: abs(x - v))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Atmeex climate entities from a config entry."""
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
    """
    Climate: температура, 7 скоростей, режим заслонки, влажность СЛАЙДЕРОМ.
    Слайдер влажности квантуется в 0/33/66/100 ↔ ступени 0..3 (если увлажнитель есть).
    """

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
        """Init climate entity.

        entry_id и refresh_device_cb нужны только в проде, в тестах могут быть None.
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

    # ---------- helpers ----------

    @property
    def _cond(self) -> dict[str, Any]:
        return self.coordinator.data.get("states", {}).get(str(self._device_id), {}) or {}

    def _has_humidifier(self) -> bool:
        stg = self._cond.get("hum_stg")
        return isinstance(stg, (int, float)) or ("hum_stg" in self._cond)

    async def _refresh(self) -> None:
        """Обновить состояние устройства через refresh_device из runtime_data.

        В тестах этот метод часто переопределяется на AsyncMock().
        """
        if callable(self._refresh_device_cb):
            await self._refresh_device_cb(self._device_id)

    # ---------- доступность ----------

    @property
    def available(self) -> bool:
        return bool(self._cond.get("online", True))

    # ---------- поддержка фич ----------

    @property
    def supported_features(self) -> int:
        features = self._base_supported
        if self._has_humidifier():
            features |= ClimateEntityFeature.TARGET_HUMIDITY
        return features

    # ---------- HVAC ----------

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode.FAN_ONLY if bool(self._cond.get("pwr_on")) else HVACMode.OFF

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.api.set_power(self._device_id, hvac_mode != HVACMode.OFF)
        await self._refresh()

    # ---------- Температура ----------

    @property
    def current_temperature(self) -> float | None:
        val = self._cond.get("temp_room")  # деци-°C
        return (val / 10) if isinstance(val, (int, float)) else None

    @property
    def target_temperature(self) -> float:
        # если цели нет, показываем текущую/20.0
        val = self._cond.get("u_temp_room")  # деци-°C (цель)
        if isinstance(val, (int, float)):
            return val / 10
        cur = self.current_temperature
        return cur if isinstance(cur, (int, float)) else 20.0

    async def async_set_temperature(self, **kwargs) -> None:
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

        if not bool(self._cond.get("pwr_on")):
            await self.api.set_power(self._device_id, True)
        await self.api.set_target_temperature(self._device_id, t_clamped)
        await self._refresh()

    # ---------- Влажность (слайдер с квантованием) ----------

    @property
    def current_humidity(self) -> int | None:
        """Текущая влажность из датчика."""
        val = self._cond.get("hum_room")
        return int(val) if isinstance(val, (int, float)) else None

    @property
    def target_humidity(self) -> int | None:
        """Показываем одно из 0/33/66/100 (по текущей ступени hum_stg)."""
        if not self._has_humidifier():
            return None
        stg = self._cond.get("hum_stg")
        if not isinstance(stg, (int, float)):
            stg = 0
        stg = max(0, min(3, int(stg)))
        return HUM_ALLOWED[stg]

    async def async_set_humidity(self, humidity: int) -> None:
        """Принимаем любое число, квантируем в 0/33/66/100 → ступень 0..3."""
        if not self._has_humidifier():
            return
        q = _quantize_humidity(humidity)
        stage = HUM_ALLOWED.index(q)  # 0..3
        await self.api.set_humid_stage(self._device_id, stage)
        await self._refresh()

    # ---------- Вентилятор ----------

    @property
    def fan_mode(self) -> str | None:
        speed = self._cond.get("fan_speed")
        if isinstance(speed, (int, float)):
            speed = int(speed)
        return FAN_MODES[speed - 1] if speed in range(1, 8) else None

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        try:
            speed = int(fan_mode)
        except (ValueError, TypeError):
            _LOGGER.warning("Unsupported fan_mode: %s", fan_mode)
            return
        await self.api.set_fan_speed(self._device_id, speed)
        await self._refresh()

    # ---------- Swing = режим бризера ----------

    @property
    def swing_mode(self) -> str | None:
        pos = self._cond.get("damp_pos")
        if isinstance(pos, int) and 0 <= pos <= 3:
            return BRIZER_SWING_MODES[pos]
        return None

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        if swing_mode not in BRIZER_SWING_MODES:
            _LOGGER.warning("Unsupported swing_mode: %s", swing_mode)
            return
        await self.api.set_brizer_mode(self._device_id, BRIZER_SWING_MODES.index(swing_mode))
        await self._refresh()

    # ---------- Атрибуты для UI/отладки ----------

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs = dict(self._cond)
        tr = self._cond.get("temp_room")
        ut = self._cond.get("u_temp_room")
        if isinstance(tr, (int, float)):
            attrs["room_temp_c"] = round(tr / 10, 1)
        if isinstance(ut, (int, float)):
            attrs["target_temp_c"] = round(ut / 10, 1)
        attrs["has_humidifier"] = self._has_humidifier()

        # expose last_success_ts from coordinator data
        data = getattr(self.coordinator, "data", {}) or {}
        ts = data.get("last_success_ts")
        if isinstance(ts, (int, float)):
            attrs["last_success_ts"] = ts
            try:
                attrs["last_success_utc"] = datetime.fromtimestamp(
                    ts, tz=timezone.utc
                ).isoformat()
            except Exception:  # pragma: no cover - defensive only
                pass

        return attrs