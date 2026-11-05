"""Общие заготовки сущностей ZOTA Smart: одно устройство на все сенсоры.

Все сущности интеграции принадлежат одному устройству «ZOTA Smart».
Идентификатор устройства — постоянная константа ``boiler``: он не зависит
от того, зарегистрировался ли модуль, поэтому ``unique_id`` и ``identifiers``
не меняются после получения серийника (иначе HA создала бы второй набор
сущностей). Серийник доступен как ``serial_number`` в ``device_info``.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME
from .coordinator import ZotaCoordinator


class ZotaEntity(CoordinatorEntity[ZotaCoordinator]):
    """Базовая сущность ZOTA Smart.

    Общая часть платформ: подписка на координатор, единое устройство и
    обработка ``available``.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ZotaCoordinator,
        description: EntityDescription,
    ) -> None:
        """Создать сущность по описанию."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_identifier}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Одно устройство «ZOTA Smart» на все сущности."""
        identifier = self.coordinator.device_identifier
        return DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            name=NAME,
            manufacturer="ZOTA",
            model="PO Smart",
            serial_number=(
                str(self.coordinator.serial)
                if self.coordinator.serial is not None
                else None
            ),
        )

    @property
    def available(self) -> bool:
        """Сущность доступна, пока есть связь с модулем.

        ``CoordinatorEntity.available`` уже отражает ``last_update_success``
        координатора, но он обновляется и при ошибке опроса — здесь нужна
        именно связь с модулем.
        """
        return self.coordinator.connected


__all__ = ["ZotaEntity"]
