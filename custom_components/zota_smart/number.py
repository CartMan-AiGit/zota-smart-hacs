"""Number-настройки клапана ZOTA Smart (cmd=0x0C).

Четыре числовые сущности: целевая температура, включение, периодичность и
полное открытие клапана. Запись каждой — полный блок настроек через
:meth:`ZotaCoordinator.send_valve_config` с одним изменённым полем; текущие
значения — из info-блока (§11.13.2).
"""

from __future__ import annotations

import asyncio

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    VALVE_FULL_OPEN_MAX,
    VALVE_FULL_OPEN_MIN,
    VALVE_MODE_FLOOR,
    VALVE_ON_TIME_MAX,
    VALVE_ON_TIME_MIN,
    VALVE_PERIOD_MAX,
    VALVE_PERIOD_MIN,
    VALVE_TARGET_FLOOR_MAX,
    VALVE_TARGET_MAX,
    VALVE_TARGET_MIN,
)
from .coordinator import ZotaConfigEntry, ZotaCoordinator
from .entity import ZotaEntity
from .state import ZotaState


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZotaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Поднять number-настройки клапана для одной записи конфигурации."""
    coordinator: ZotaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ZotaValveNumber(coordinator, description)
        for description in VALVE_NUMBERS
    )


VALVE_NUMBERS: tuple[NumberEntityDescription, ...] = (
    NumberEntityDescription(
        key="valve_target",
        name="Клапан: целевая температура",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=VALVE_TARGET_MIN,
        native_max_value=VALVE_TARGET_MAX,
        native_step=1,
        icon="mdi:thermometer",
    ),
    NumberEntityDescription(
        key="valve_on_time",
        name="Клапан: включение",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        native_min_value=VALVE_ON_TIME_MIN,
        native_max_value=VALVE_ON_TIME_MAX,
        native_step=1,
        icon="mdi:timer-play",
    ),
    NumberEntityDescription(
        key="valve_period",
        name="Клапан: периодичность",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        native_min_value=VALVE_PERIOD_MIN,
        native_max_value=VALVE_PERIOD_MAX,
        native_step=1,
        icon="mdi:timer-refresh",
    ),
    NumberEntityDescription(
        key="valve_full_open",
        name="Клапан: полное открытие",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        native_min_value=VALVE_FULL_OPEN_MIN,
        native_max_value=VALVE_FULL_OPEN_MAX,
        native_step=1,
        icon="mdi:valve-open",
    ),
)


class ZotaValveNumber(ZotaEntity, NumberEntity):
    """Number-настройка клапана (cmd=0x0C, одно поле)."""

    entity_description: NumberEntityDescription

    _attr_mode = NumberMode.SLIDER

    @property
    def _state(self) -> ZotaState | None:
        """Последнее состояние котла."""
        return self.coordinator.data

    @property
    def native_value(self) -> float | None:
        """Текущее значение поля из info-блока.

        Для целевой температуры — значение активного контура (§11.13.6):
        в режиме «Тёплый пол» это поле ``[10]``, иначе ``[7]``.
        """
        state = self._state
        if state is None:
            return None
        if self.entity_description.key == "valve_target":
            return float(state.valve_target_active)
        return float(getattr(state, self.entity_description.key))

    @property
    def native_max_value(self) -> float:
        """Потолок целевой: 50 при активном Тёплом поле, иначе 90 (§11.13.6)."""
        if self.entity_description.key == "valve_target":
            state = self._state
            if state is not None and state.valve_mode == VALVE_MODE_FLOOR:
                return float(VALVE_TARGET_FLOOR_MAX)
            return float(VALVE_TARGET_MAX)
        return float(self.entity_description.native_max_value)

    async def async_set_native_value(self, value: float) -> None:
        """Записать поле и дождаться подтверждения котла.

        Key сущности ``valve_<field>`` соответствует атрибуту ``ZotaState``
        (например ``valve_target``); поле write-блока — без префикса
        (``target``).
        """
        field = self.entity_description.key.removeprefix("valve_")
        try:
            result = await asyncio.wrap_future(
                self.coordinator.send_valve_config(**{field: int(value)})
            )
        except Exception as err:
            raise HomeAssistantError("котёл не подтвердил команду: %s" % err) from err
        if result != 0:
            raise HomeAssistantError("котёл не подтвердил команду")
        # Ускоренный опрос: обновлённое состояние нужно как можно скорее.
        self.coordinator.poll_now()


__all__ = [
    "VALVE_NUMBERS",
    "ZotaValveNumber",
    "async_setup_entry",
]
