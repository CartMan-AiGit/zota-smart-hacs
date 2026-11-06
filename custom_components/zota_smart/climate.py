"""Климатическая сущность ZOTA Smart: ступень мощности и уставка.

Котёл не имеет «мягких» режимов нагрева — вместо них у него есть
**ступень мощности** (cmd=1, 0…3) и **уставка температуры подачи**
(cmd=2, 30…90 °C) — PROTOCOL.md §11.7. Поэтому климат отдаёт:

* ``preset_mode`` — выбранную ступень («Выкл» / «5 кВт» / «10 кВт» / «15 кВт»);
* ``target_temperature`` — уставку;
* ``hvac_mode`` — ``OFF``, если ступень 0, иначе ``HEAT``.

``HEAT`` по умолчанию включает первую ступень (5 кВт): включать «нагрев»
без указания мощности нельзя, а ненулевая ступень — минимально осмысленный
вариант. Последняя выбранная ненулевая ступень запоминается, поэтому
переключение ``OFF`` → ``HEAT`` возвращает ту же мощность.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityDescription,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CMD_SETPOINT,
    CMD_STAGE,
    DOMAIN,
    SETPOINT_MAX,
    SETPOINT_MIN,
    TEMP_SENSOR_OUTSIDE,
    TEMP_SENSOR_RETURN,
    TEMP_SENSOR_ROOM,
    build_stage_presets,
)
from .coordinator import ZotaConfigEntry, ZotaCoordinator
from .entity import ZotaEntity
from .state import ZotaState


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZotaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Поднять климатическую сущность для одной записи конфигурации."""
    coordinator: ZotaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ZotaClimate(coordinator, CLIMATE_DESCRIPTION)])


CLIMATE_DESCRIPTION = ClimateEntityDescription(
    key="climate",
    name="Котёл",
)


class ZotaClimate(ZotaEntity, ClimateEntity):
    """Климат ZOTA Smart: ступень мощности + уставка подачи."""

    entity_description: ClimateEntityDescription

    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_temperature_unit = "°C"
    _attr_min_temp = SETPOINT_MIN
    _attr_max_temp = SETPOINT_MAX
    _attr_target_temperature_step = 1
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )

    def __init__(
        self,
        coordinator: ZotaCoordinator,
        description: ClimateEntityDescription,
    ) -> None:
        """Создать климат и подготовить состояние по умолчанию."""
        super().__init__(coordinator, description)
        #: Пресеты строятся из мощности котла в конфигурации записи:
        #: «Выкл» + ступени P/3, 2P/3, P кВт (для 15 кВт — прежние 5/10/15).
        self._presets: tuple[str, ...] = build_stage_presets(coordinator.power_kw)
        self._attr_preset_modes = list(self._presets)
        #: Последняя ненулевая ступень — в неё возвращается режим HEAT.
        self._last_stage: int = 1

    # --- Текущее состояние --------------------------------------------------

    @property
    def _state(self) -> ZotaState | None:
        """Последнее состояние котла."""
        return self.coordinator.data

    @property
    def current_temperature(self) -> float | None:
        """Текущая температура по выбранному в настройках датчику, °C.

        Источник — настройка записи ``temp_sensor`` (координатор): подача
        (дефолт, датчик 3), встроенный датчик колбы (``[15..16]``), датчик
        помещения или улицы. Если
        выбранный внешний датчик не подключён (слот 0x4001 → ``None``),
        климат честно показывает «нет данных».
        """
        state = self._state
        if state is None:
            return None
        source = self.coordinator.temp_sensor
        if source == TEMP_SENSOR_RETURN:
            return float(state.temp_return)
        if source == TEMP_SENSOR_ROOM:
            return state.sensor_room
        if source == TEMP_SENSOR_OUTSIDE:
            return state.sensor_outside
        return float(state.temp_supply)

    @property
    def target_temperature(self) -> float | None:
        """Уставка температуры подачи, °C."""
        state = self._state
        if state is None:
            return None
        return state.setpoint

    @property
    def hvac_mode(self) -> HVACMode:
        """Режим: ``OFF`` на нулевой ступени, иначе ``HEAT``.

        Ступень — установленная (уставка, поле ``[4]`` info-блока), а не
        текущая: пресет отражает разрешённую мощность, а не текущую.
        """
        state = self._state
        if state is None or state.stage == 0:
            return HVACMode.OFF
        return HVACMode.HEAT

    @property
    def preset_mode(self) -> str | None:
        """Пресет мощности по установленной ступени (уставке, ``[4]``)."""
        state = self._state
        if state is None:
            return None
        return self._presets[state.stage] if 0 <= state.stage < len(self._presets) else None

    # --- Команды ------------------------------------------------------------

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Записать уставку температуры подачи (cmd=2)."""
        temperature = kwargs.get("temperature")
        if temperature is None:
            raise HomeAssistantError("не задана температура")
        value = int(round(float(temperature)))
        if not SETPOINT_MIN <= value <= SETPOINT_MAX:
            raise HomeAssistantError(
                "уставка вне диапазона %d…%d °C" % (SETPOINT_MIN, SETPOINT_MAX)
            )
        await self._write(CMD_SETPOINT, value)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Записать ступень мощности (cmd=1) по имени пресета."""
        if preset_mode not in self._presets:
            raise HomeAssistantError("неизвестный пресет %r" % preset_mode)
        stage = self._presets.index(preset_mode)
        await self._write(CMD_STAGE, stage)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Включить/выключить нагрев через ступень мощности (cmd=1)."""
        if hvac_mode == HVACMode.OFF:
            await self._write(CMD_STAGE, 0)
            return
        if hvac_mode != HVACMode.HEAT:
            raise HomeAssistantError("режим %s не поддерживается" % hvac_mode)
        await self._write(CMD_STAGE, self._last_stage)

    # --- Внутреннее ---------------------------------------------------------

    async def _write(self, cmd: int, value: int) -> None:
        """Отправить запись и дождаться подтверждения котла."""
        try:
            result = await asyncio.wrap_future(self.coordinator.send_write(cmd, value))
        except Exception as err:
            raise HomeAssistantError("котёл не подтвердил команду: %s" % err) from err
        if result != 0:
            raise HomeAssistantError("котёл не подтвердил команду")
        if cmd == CMD_STAGE and value > 0:
            self._last_stage = value
        # Ускоренный опрос: обновлённое состояние нужно как можно скорее.
        self.coordinator.poll_now()


__all__ = ["CLIMATE_DESCRIPTION", "ZotaClimate", "async_setup_entry"]
