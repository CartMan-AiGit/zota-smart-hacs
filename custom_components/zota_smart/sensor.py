"""Read-сенсоры ZOTA Smart: температуры, уставка, ступень, мощность, ошибки."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
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
    """Поднять сенсоры для одной записи конфигурации."""
    coordinator: ZotaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ZotaTemperatureSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
    )


SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="temp_supply",
        name="Датчик 3: вода (подача)",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="°C",
    ),
    SensorEntityDescription(
        key="temp_return",
        name="Обратка",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="°C",
    ),
    SensorEntityDescription(
        key="setpoint",
        name="Уставка",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="°C",
    ),
    SensorEntityDescription(
        key="sensor_room",
        name="Датчик 1: помещение",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="°C",
    ),
    SensorEntityDescription(
        key="sensor_outside",
        name="Датчик 2: улица",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="°C",
    ),
    SensorEntityDescription(
        key="sensor_air",
        name="Датчик 4: воздух",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="°C",
    ),
    SensorEntityDescription(
        key="stage",
        name="Ступень",
        icon="mdi:heating-coil",
    ),
    SensorEntityDescription(
        key="power_active_kw",
        name="Мощность: текущая",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="kW",
    ),
    SensorEntityDescription(
        key="power_kw",
        name="Мощность: уставка",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="kW",
    ),
    SensorEntityDescription(
        key="errors",
        name="Ошибки",
        icon="mdi:alert-circle-outline",
    ),
)


class ZotaTemperatureSensor(ZotaEntity, SensorEntity):
    """Сенсор ZOTA Smart.

    Имя класса — историческое; платформа публикует все описания из
    :data:`SENSOR_DESCRIPTIONS`, а не только температуры.

    Наследует ``SensorEntity`` — без него HA не знает, что ``native_value``
    это значение сенсора, и состояние отображается как «неизвестно».
    """

    entity_description: SensorEntityDescription

    @property
    def native_value(self):
        """Текущее значение по ключу описания.

        Мощность разделена на два сенсора, потому что котёл регулирует
        нагрев по целевой температуре:

        * «Мощность: текущая» — фактически включённые ТЭНы (текущая
          ступень ``[3]``): при подходе к уставке падает 15 → 10 → 5 → 0;
        * «Мощность: уставка» — установленная ступень-ограничитель
          (``[4]``), зеркало пресета климатической сущности.

        Раскладка ``[3]``/``[4]`` — по живой валидации 2026-11-05 (веб ZOTA).
        """
        state: ZotaState | None = self.coordinator.data
        if state is None:
            return None
        key = self.entity_description.key
        if key == "temp_supply":
            return state.temp_supply
        if key == "temp_return":
            return state.temp_return
        if key == "sensor_room":
            return state.sensor_room
        if key == "sensor_outside":
            return state.sensor_outside
        if key == "sensor_air":
            return state.sensor_air
        if key == "setpoint":
            return state.setpoint
        if key == "stage":
            return state.stage_name
        if key == "power_active_kw":
            return state.power_active_kw
        if key == "power_kw":
            return state.power_kw
        if key == "errors":
            return state.errors_summary
        return None


__all__ = ["SENSOR_DESCRIPTIONS", "ZotaTemperatureSensor", "async_setup_entry"]
