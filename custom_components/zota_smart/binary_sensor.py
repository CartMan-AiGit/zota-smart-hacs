"""Бинарные сенсоры ZOTA Smart: нагрев, насосы.

Признаки насосов берутся из маски ``[37]`` info-блока; для насоса 1
приоритет у поля ``[29]`` (§11.12.2) — это сделано в чистой логике
(:mod:`.state`), здесь разбирается только выдача значения.

Признак «Нагрев» — это ``active_stage > 0`` (текущая/активная ступень ``[3]``
info-блока), а не бит 7 маски ``[37]``: живая валидация 2026-11-05 показала,
что при греющем котле (нагревательные группы включены, вода греется) бит 7
остаётся 0, и индикатор показывал «Не работает».
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ZotaConfigEntry, ZotaCoordinator
from .entity import ZotaEntity
from .state import ZotaState


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZotaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Поднять бинарные сенсоры для одной записи конфигурации."""
    coordinator: ZotaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ZotaBinarySensor(coordinator, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


BINARY_SENSOR_DESCRIPTIONS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="heating",
        name="Нагрев",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    BinarySensorEntityDescription(
        key="pump1",
        name="Насос 1",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:pump",
    ),
    BinarySensorEntityDescription(
        key="pump2",
        name="Насос 2",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:pump",
    ),
)


class ZotaBinarySensor(ZotaEntity, BinarySensorEntity):
    """Бинарный сенсор ZOTA Smart по одному из полей состояния.

    Наследует ``BinarySensorEntity`` — без него HA не знает, что ``is_on``
    это состояние бинарного сенсора, и оно отображается как «неизвестно».
    """

    entity_description: BinarySensorEntityDescription

    @property
    def is_on(self) -> bool | None:
        """Состояние признака."""
        state: ZotaState | None = self.coordinator.data
        if state is None:
            return None
        key = self.entity_description.key
        if key == "heating":
            return state.heating
        if key == "pump1":
            return state.pump1
        if key == "pump2":
            return state.pump2
        return None


__all__ = [
    "BINARY_SENSOR_DESCRIPTIONS",
    "ZotaBinarySensor",
    "async_setup_entry",
]
